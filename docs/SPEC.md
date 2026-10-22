# WhyDown

**Tagline:** Not just *if* a website is down, but *why*.

**Project type:** Integrated project
**Subjects:** Cloud Computing · Data Communication & Networking · Software Testing & Quality Assurance
**Hosting constraint:** Strictly free tiers (no pay-as-you-go services anywhere)

---

## 1. Overview

WhyDown is a website uptime and network diagnostics monitor. A user registers a list of URLs (a college portal, a small shop site, personal projects). On a fixed schedule, the system checks each URL and breaks the request into network stages: **DNS lookup, TCP connect, TLS handshake, time to first byte (TTFB), and total response time**.

When a check fails or becomes slow, WhyDown does not just report "down". It classifies the failure (DNS problem, connection refused, timeout, certificate error, HTTP error, slow response), records it, opens an incident, and sends an alert. A dashboard shows uptime percentages, response-time history and a per-stage timing breakdown.

## 2. Problem Statement

Typical uptime tools answer one question: "is it up or down?" For small businesses, students and developers the more useful question is **why**, and where in the network path the problem is. A slow DNS server, an expired certificate and an overloaded web server all look the same ("site not loading") to the owner, but they have different fixes.

Paid tools that do this are out of reach for many small users. WhyDown aims to be a simple, free-to-run alternative that gives a diagnosis, not only a status.

## 3. Objectives

1. Monitor multiple URLs on a schedule and store the results.
2. Measure and display network timing broken down by stage (DNS, TCP, TLS, TTFB, total).
3. Classify failures into meaningful categories.
4. Detect incidents (down and recovery) and send alerts without spamming.
5. Provide a dashboard with uptime %, history and diagnostics.
6. Run entirely on free-tier services without any risk of billing.
7. Verify correctness with a documented test strategy, a controllable mock server and a coverage report.

## 4. Scope

### In scope
- Up to ~20 monitored URLs (HTTP/HTTPS GET checks)
- Scheduled checks every ~10 minutes
- Timing breakdown and failure classification
- Incident tracking and alerts (email or Telegram)
- Dashboard (uptime %, response-time chart, per-site details, incident list)
- Automated tests, mock failure server, CI-style test run
- Deployment on free tiers

### Out of scope (possible future work)
- Checks more often than every few minutes
- Non-HTTP protocols (ICMP ping, raw TCP ports, SMTP)
- Multi-user accounts and billing
- Multi-region probes
- Mobile app

## 5. How the Three Subjects Are Integrated

| Subject | What it does in WhyDown |
|---|---|
| **Cloud Computing** | Multi-service cloud deployment on free tiers: web service (Render), managed database (free Postgres), external scheduler (cron-job.org), environment-based configuration and secrets, automated deployment from a code repository, data retention to stay within free limits, and analysis of cold-start behaviour of a free instance. |
| **Data Communication & Networking** | The core of the project: DNS resolution, TCP three-way handshake timing, TLS handshake and certificate validation, HTTP request/response timing, status codes, timeouts and failure localisation. Optional traceroute/ping reasoning in the report. |
| **Software Testing & QA** | Test plan, unit tests for the timing and classification logic, integration tests for the pipeline and database, a mock server that can be made slow, broken, or insecure on demand, alert-logic tests (including no false or duplicate alerts), coverage report, and defect log. |

## 6. System Architecture

```
                 +------------------+
                 |   cron-job.org   |   (free external scheduler)
                 |  every 10 minutes|
                 +--------+---------+
                          | HTTPS GET /run-checks (secret token)
                          v
+--------------------------------------------------------+
|          Render free web service  (Flask app)          |
|                                                        |
|  /run-checks  -> checker engine (thread pool)          |
|                    DNS | TCP | TLS | HTTP timing       |
|                    classifier -> incident logic         |
|                    alert sender                         |
|  /             -> dashboard (HTML + Chart.js)          |
|  /api/...      -> JSON API                             |
+-------------+-------------------------+----------------+
              |                         |
              v                         v
   +---------------------+     +------------------------+
   |  Free Postgres DB   |     | Email / Telegram alert |
   | (external to Render)|     +------------------------+
   +---------------------+
```

### Component summary
- **Flask app:** one deployable service containing the checker, API and dashboard.
- **Scheduler:** cron-job.org calls the protected `/run-checks` endpoint. This both triggers checks and keeps the free Render instance awake.
- **Database:** an external free Postgres (Neon or Supabase; verify current free limits before choosing). Render's own free Postgres expires after 30 days and SQLite on a free Render instance loses data on restart, so neither is used.
- **Alerts:** Telegram bot (simplest) or SMTP email.

## 7. Free-Tier Plan and Constraints

| Item | Constraint | How WhyDown handles it |
|---|---|---|
| Render free web service sleeps after ~15 min without traffic | Cold start of about a minute | The scheduler pings every 10 minutes, so it stays awake; cold-start time is measured and reported as a finding. |
| Render free instance hours (750/month per workspace) | One always-on service uses nearly all of it | Run only one Render service. |
| Render free Postgres expires after 30 days | Data would disappear | Use an external free database. |
| No persistent disk on free web service | Local files are lost on restart | Store all data in the database. |
| Limited CPU/RAM on free instance | Slow if checks are sequential | Use a small thread pool, short per-check timeouts. |
| Free database storage limits | Table growth over time | Purge or aggregate old check rows (for example older than 30 days). |
| Scheduler request timeout | A long-running request may be cut off | Keep `/run-checks` fast (concurrent checks) or return immediately and run checks in a background thread. |

> Limits on any third-party free plan can change. Check each provider's current free-plan page before depending on a number.

## 8. Functional Requirements

### 8.1 Site management
- Add, edit, disable and delete monitored sites (name, URL, expected status code, optional keyword to find in the response, slow threshold).
- Validate URLs (scheme http/https only, hostname required).
- Enforce a maximum number of sites.

### 8.2 Checking
For each enabled site, on each run:
1. **DNS:** resolve the hostname and record resolution time and the resolved IP.
2. **TCP:** open a socket to the IP and port and record connect time.
3. **TLS (HTTPS only):** perform the handshake, record its time, and read certificate details (expiry date, issuer, hostname match).
4. **HTTP:** send a GET request, record time to first byte and total time, status code and response size.
5. Optional **keyword check** to detect a page that loads but shows an error.
6. Each stage has its own timeout; a failure at any stage stops the check and is classified.

### 8.3 Failure classification

| Code | Meaning | Typical cause |
|---|---|---|
| `OK` | All stages succeeded, status as expected, within threshold | Healthy |
| `SLOW` | Succeeded but total time or a stage exceeded the threshold | Overload, congestion, slow DNS |
| `DNS_FAILURE` | Hostname could not be resolved | Domain expired, DNS misconfigured, DNS server down |
| `DNS_SLOW` | DNS succeeded but took unusually long | Slow resolver |
| `CONNECT_TIMEOUT` | TCP connection did not complete in time | Server down, firewall dropping packets |
| `CONNECTION_REFUSED` | Server actively rejected the connection | Service not running on that port |
| `BLOCKED_TARGET` | Resolved IP was private/loopback/link-local (SSRF) blocked | Internal address, SSRF attempt blocked |
| `TLS_ERROR` | Handshake failed for a non-certificate reason | Protocol mismatch, bad configuration |
| `CERT_EXPIRED` | Certificate expired (X509 verify code CERT_HAS_EXPIRED) | Missed renewal |
| `CERT_INVALID` | Certificate failed verification (hostname mismatch, untrusted, ...) | Wrong certificate, self-signed |
| `READ_TIMEOUT` | Connected but no response in time (includes dropped connection) | Application hung |
| `HTTP_3XX` | A 3xx was returned and did not match expected_status (no redirect followed) | Misconfiguration, unexpected redirect |
| `HTTP_4XX` / `HTTP_5XX` | Unexpected 4xx/5xx status code | Client or server error |
| `INTERNAL_ERROR` | The check raised an unexpected exception (bug or environment issue) | Code defect |
| `KEYWORD_MISSING` | Page loaded but expected text absent | Broken page, wrong content |

### 8.3.1 Implementation notes

- **`total_ms`** is the time of the whole check (DNS + TCP + TLS + HTTP), not just
  the HTTP stage. This is what the `SLOW` threshold compares against.
- **Stage-level thresholds**: when DNS takes longer than the slow threshold the result
  is `DNS_SLOW`; otherwise `SLOW` is reported when the total exceeds the threshold
  (a single slow stage also pushes `SLOW` since it inflates the total).
- **Safety net**: `check_url()` never raises — any unexpected exception is caught,
  logged and classified as `INTERNAL_ERROR`, so one bad site can never stop a run
  (spec section 10).

> **Certificate-expiry warning is a flag, not a result code.** Instead of a
> `CERT_EXPIRING` classification, the checker records `cert_expires_at` (parsed
> with `ssl.cert_time_to_seconds()` into a timestamp) and a boolean
> `cert_expiring_soon` flag (expiry within `CERT_EXPIRY_WARNING_DAYS`, env-configurable
> and no longer hardcoded to 7). A site whose certificate is expiring soon keeps its
> `OK`/`SLOW` result; alerting uses the flag and fires once per threshold crossing.

### 8.4 Incidents and alerts
- A site enters an **incident** after **N consecutive failures** (default 3) to avoid alerts on a single blip.
- One **DOWN alert** is sent when the incident opens, including the failure code and the stage where it failed.
- One **RECOVERY alert** is sent on the first successful check afterwards, including the duration of the outage.
- No repeated alerts while the incident remains open (optionally a reminder after a long time).
- Certificate-expiry warnings are sent once per threshold crossing.
- Alert state is stored in the database so a restart does not cause duplicates.

### 8.5 Dashboard
- **Overview:** all sites with current status, last check time, 24 h and 7 d uptime %.
- **Site detail:** response-time line chart, stacked bar of DNS/TCP/TLS/TTFB, status history, certificate expiry, resolved IP.
- **Incidents:** list with start time, end time, duration and failure type.
- **Admin forms:** add/remove sites, protected by a simple admin password or token.

## 9. Data Model

**sites**
| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| name | text | |
| url | text | |
| expected_status | integer | default 200 |
| keyword | text null | optional |
| slow_threshold_ms | integer | |
| enabled | boolean | |
| created_at | timestamp | |

**checks**
| Column | Type | Notes |
|---|---|---|
| id | bigint PK | |
| site_id | FK → sites | |
| checked_at | timestamp | |
| status_code | integer null | |
| result | text | classification code |
| failed_stage | text null | dns / tcp / tls / http |
| dns_ms, tcp_ms, tls_ms, ttfb_ms, total_ms | integer null | |
| resolved_ip | text null | |
| cert_expires_at | timestamp null | |
| error_detail | text null | |

**incidents**
| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| site_id | FK → sites | |
| started_at | timestamp | |
| ended_at | timestamp null | null while open |
| failure_type | text | |
| consecutive_failures | integer | |

**alert_log**
| Column | Type | Notes |
|---|---|---|
| id | integer PK | |
| incident_id | FK null | |
| site_id | FK | |
| kind | text | down / recovery / cert_warning |
| sent_at | timestamp | |
| channel | text | telegram / email |
| success | boolean | |

Index `checks(site_id, checked_at)` for dashboard queries.

## 10. Check Engine Design

- Use Python's `socket`, `ssl` and `http.client` (or `requests` plus manual stage timing) so that each stage can be timed separately.
- Measure with a monotonic clock (`time.perf_counter`).
- Run sites concurrently with a thread pool (for example 5 to 10 workers) and a hard per-site timeout (for example 8 to 10 seconds in total).
- Keep the networking logic in small pure functions (`resolve_dns`, `connect_tcp`, `do_tls`, `do_http`, `classify`) so they can be unit-tested with mocks.
- Never let one site's exception stop the run; catch and classify every error.
- Each run records a run summary (sites checked, failures, duration).

## 11. API Endpoints

| Method | Path | Purpose | Access |
|---|---|---|---|
| GET | `/run-checks?token=...` | Trigger a check cycle (called by scheduler) | Secret token |
| GET | `/health` | Lightweight health check, no DB or network work | Public |
| GET | `/api/sites` | List sites with current status | Public (read-only) |
| GET | `/api/sites/<id>/checks` | Recent check history | Public (read-only) |
| GET | `/api/incidents` | Incident list | Public (read-only) |
| POST | `/api/sites` | Add a site | Admin |
| PUT/DELETE | `/api/sites/<id>` | Edit or remove a site | Admin |
| GET | `/` | Dashboard | Public |

## 12. Security Considerations

- **Protect `/run-checks`** with a secret token so strangers cannot trigger checks.
- **Prevent server-side request forgery (SSRF):** because the app fetches user-supplied URLs, block private, loopback and link-local addresses (for example 127.0.0.0/8, 10.0.0.0/8, 192.168.0.0/16, 169.254.0.0/16) after DNS resolution, and allow only http/https.
- **Limit** the number of sites, response size read, redirects followed and timeouts.
- **Secrets** (database URL, token, bot credentials) live in environment variables, never in the repository.
- **Admin routes** require authentication; use parameterised queries to avoid SQL injection; escape output on the dashboard.
- Do not store response bodies, only metadata.

## 13. Testing and QA Plan

### 13.1 Test strategy
| Level | What is tested | Tools |
|---|---|---|
| Unit | Failure classifier, timing calculations, URL validation, incident state machine, alert rules, SSRF filter | pytest, mocks |
| Integration | Checker against the mock server, database reads/writes, full run cycle | pytest, test database |
| API | Endpoint responses, auth on protected routes, input validation | Flask test client |
| System / end-to-end | Deployed app with the scheduler: failure detected, alert received, dashboard updated | Manual script plus checklist |
| Performance / load | Run duration with 20 sites, behaviour with slow sites, dashboard response time | Simple scripts or k6 / Locust |
| Non-functional | Free-tier limits, cold start time, recovery after restart | Measurements |

### 13.2 Mock failure server
A small local server you control, with endpoints such as:
- `/ok` returns 200 quickly
- `/slow?ms=3000` delays the response
- `/status/500` and `/status/404` return those codes
- `/drop` accepts the connection and closes it without replying
- `/hang` accepts the connection and never replies (read timeout)
- `/keyword-missing` returns 200 with a page that lacks the expected text
- A switch to turn the whole mock site "down" and "up" during a test (to test incident open and recovery)

For certificate cases, use a local server with a self-signed certificate (invalid), an expired certificate and a hostname-mismatch certificate, or public test hosts such as badssl.com for expired / wrong-host / self-signed scenarios.

### 13.3 Example test cases

| ID | Scenario | Expected result |
|---|---|---|
| T01 | Healthy site | `OK`, all stage times recorded |
| T02 | Non-existent domain | `DNS_FAILURE`, failed stage = dns |
| T03 | Closed port | `CONNECTION_REFUSED` |
| T04 | Server accepts but never responds | `READ_TIMEOUT` |
| T05 | Response slower than threshold | `SLOW` |
| T06 | HTTP 500 | `HTTP_5XX` |
| T07 | Expired certificate | `CERT_EXPIRED` |
| T08 | Hostname mismatch certificate | `CERT_INVALID` |
| T09 | Certificate expiring in under N days | `cert_expiring_soon` flag set (result stays OK/SLOW), one alert only |
| T10 | One failure then success | No incident opened |
| T11 | N consecutive failures | Incident opened, exactly one DOWN alert |
| T12 | Failures continue | No repeated alerts |
| T13 | First success after incident | Incident closed, exactly one RECOVERY alert with duration |
| T14 | App restarts mid-incident | No duplicate alert (state in DB) |
| T15 | URL pointing to `127.0.0.1` or private range | Rejected by SSRF filter |
| T16 | `/run-checks` without token | 401/403, no checks run |
| T17 | Invalid URL submitted | 400 with clear error |
| T18 | 20 sites with 5 slow ones | Run finishes within the time budget |
| T19 | Alert channel unreachable | Failure logged, check run not broken |
| T20 | Keyword missing on a 200 page | `KEYWORD_MISSING` |

### 13.4 Quality measures
- Coverage report with `pytest-cov`; target **80%+** overall and near-complete coverage of the classifier and incident logic.
- Test plan document, test case table with pass/fail results, and a defect log (bug found, cause, fix).
- Tests run automatically before deployment (a simple script or CI step).
- Lint/format checks (for example `ruff` or `flake8`).

## 14. Networking Analysis for the Report

Use the collected data to discuss:
- How DNS, TCP, TLS and server time each contribute to total load time, with real measurements per site.
- Effect of **time of day** and **geography** (all checks come from the Render server's location, which is a limitation worth stating).
- How a failure at each stage looks different (DNS vs timeout vs refused vs certificate).
- TLS handshake cost compared to plain TCP.
- **Cold start of the free Render instance** measured with and without the scheduler pinging it.
- Where timeouts should be set and why (trade-off between false alarms and slow detection).
- Optional: compare results with command-line tools (`ping`, `traceroute`/`tracert`, `nslookup`, `curl -w`) to validate WhyDown's numbers.

## 15. Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3 |
| Web framework | Flask |
| Networking | `socket`, `ssl`, `http.client` (or `requests` with timing hooks) |
| Database | Free external Postgres (Neon or Supabase; verify limits), accessed with `psycopg` or SQLAlchemy |
| Frontend | Jinja2 templates, HTML/CSS, Chart.js |
| Hosting | Render free web service |
| Scheduler | cron-job.org |
| Alerts | Telegram Bot API or SMTP |
| Testing | pytest, pytest-cov, Flask test client, mock server |
| Tooling | `ruff`/`flake8`, Postman or curl for manual API tests |

## 16. Suggested Repository Structure

```
whydown/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # environment configuration
│   ├── routes.py            # dashboard + API + /run-checks
│   ├── checker.py           # DNS/TCP/TLS/HTTP stage functions
│   ├── classifier.py        # failure classification
│   ├── incidents.py         # incident state machine
│   ├── alerts.py            # Telegram / email senders
│   ├── security.py          # URL validation, SSRF filter
│   ├── db.py                # database access
│   └── templates/           # dashboard HTML
├── tests/
│   ├── test_classifier.py
│   ├── test_incidents.py
│   ├── test_security.py
│   ├── test_checker_integration.py
│   ├── test_api.py
│   └── mock_server.py
├── docs/                    # test plan, report, screenshots
├── requirements.txt
├── render.yaml              # Render service definition
├── .env.example
└── README.md
```

## 17. Deployment Steps (Outline)

1. Create a free Postgres database and copy its connection string.
2. Put the project in a code repository (Render deploys from one).
3. Create a Render **free** web service from the repository; set the start command (for example `gunicorn`) and environment variables: `DATABASE_URL`, `CRON_SECRET`, `ADMIN_PASSWORD`, alert credentials.
4. Create the database tables (a one-time init script or migration).
5. Create a cron-job.org account and add a job calling `https://<your-service>.onrender.com/run-checks?token=<secret>` every 10 minutes.
6. Add sites, wait for a few cycles, confirm checks and the dashboard.
7. Trigger a failure (point a monitored site at the mock server or a known-bad test host) and confirm the alert arrives.
8. Run the test suite and generate the coverage report.

## 18. Project Phases

| Phase | Work | Output |
|---|---|---|
| 1. Foundations | Set up project, environment, database schema, config | Running skeleton |
| 2. Checker core | DNS/TCP/TLS/HTTP timing, classification | Working checker with unit tests |
| 3. Storage and dashboard | Save results, uptime calculations, charts | Dashboard with real data |
| 4. Incidents and alerts | State machine, Telegram/email alerts | Reliable, non-spammy alerts |
| 5. Security hardening | SSRF filter, token auth, input validation | Security tests passing |
| 6. QA | Mock server, full test suite, coverage, load test | Test report |
| 7. Deployment and measurement | Render + database + scheduler, collect data over several days | Live system and measurements |
| 8. Report and demo | Write-up, screenshots, demo rehearsal | Final deliverables |

(Adjust the duration of each phase to the actual deadline.)

## 19. Deliverables

- Working deployed application (public URL)
- Source code repository with README and setup instructions
- Project report covering design, networking analysis, cloud deployment and testing
- Test plan, test case results, coverage report and defect log
- Architecture diagram and database diagram
- Demo (live or recorded) and presentation slides

## 20. Demo Script (Suggested)

1. Show the dashboard with several healthy sites and their timing breakdown.
2. Add a new site live and show its first check.
3. Switch the mock site to a failure (for example connection refused).
4. Show the incident opening and the alert arriving on the phone.
5. Restore the mock site and show the recovery alert with outage duration.
6. Show a certificate-expired test host being classified correctly.
7. Run the test suite and show the coverage report.
8. Explain how the whole system runs on free tiers.

## 21. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Free instance sleeps and checks are missed | Gaps in data | External scheduler pings every 10 minutes; record gaps and report them honestly |
| Free-plan terms change | Deployment breaks | Verify limits early; keep config portable (environment variables, standard Postgres) |
| Free database fills up | Writes fail | Data retention job; store only metadata |
| Scheduler request times out | Run reported as failed | Concurrent checks and short timeouts; or background execution |
| False alarms from transient errors | Alert fatigue | N-consecutive-failure rule, sensible timeouts |
| App abused to scan internal networks (SSRF) | Security issue | URL validation and private-IP blocking after DNS resolution |
| Third-party test hosts change | Tests become flaky | Prefer your own mock server; use public hosts only for optional checks |
| Single check location | Misleading "down" results | State this limitation; mention multi-region probes as future work |

## 22. Future Enhancements

- Multiple probe locations (for "down from here only" detection)
- Additional check types (ICMP ping, port checks, DNS record checks)
- Public status page
- Response-time anomaly detection
- Webhook alerts (Discord, Slack)
- User accounts and per-user site lists
- Exportable reports (CSV/PDF)

## 23. Glossary

- **DNS:** system that turns a domain name into an IP address.
- **TCP handshake:** the three-step exchange that opens a connection to a server.
- **TLS handshake:** the negotiation that sets up an encrypted HTTPS connection and verifies the certificate.
- **TTFB (time to first byte):** time from sending a request to receiving the first byte of the response.
- **Uptime %:** share of checks in a period that were successful.
- **Incident:** a period during which a site is considered down, from first confirmed failure to recovery.
- **SSRF:** an attack that makes a server send requests to places the attacker should not reach.
- **Cold start:** the delay when a sleeping free instance has to start up again.

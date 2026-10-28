# WhyDown Test Plan

## Executive Summary
- **Tests passed:** 931
- **Tests failed:** 0
- **Tests skipped:** 0
- **Overall test coverage:** 96% (2,654 statements total, 116 missing)
- **Critical modules tested:** checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%; security.py at 93%
- **Missing coverage:** 116 statements (4.4% total)

## Test Strategy

WhyDown implements a comprehensive testing strategy aligned with the three subjects of the project:

### Software Testing & QA (Subject 3)
- Unit tests for timing and classification logic
- Integration tests for pipeline and database
- Mock server that can be made slow, broken, or insecure
- Alert logic tests (including no false/duplicate alerts)
- Full test suite with coverage report

### Data Communication & Networking (Subject 2)
- Network stage functions: DNS, TCP, TLS, HTTP
- Failure classification and error handling
- SSRF protection and security validation

### Cloud Computing (Subject 1)
- Free-tier deployment on Render
- Database connection and data persistence
- Scheduled checks via external scheduler

## Test Structure

### Unit Tests
- **app/checker.py:** DNS resolution, TCP connection, TLS handshake, HTTP request (100% coverage)
- **app/classifier.py:** Failure classification (100% coverage)
- **app/security.py:** SSRF filter (93% coverage, 6 missing statements)
- **app/incidents.py:** Incident state machine (100% coverage)
- **app/alerts.py:** Telegram alert sending (100% coverage)
- **app/runner.py:** Full check runner (98% coverage)
- **app/db.py:** PostgreSQL access layer (96% coverage)
- **app/config.py:** Environment configuration (96% coverage)
- **app/auth.py:** Admin authentication (96% coverage)
- **app/routes.py:** All Flask routes (89% coverage)

### Integration Tests
- **tests/test_checker_integration.py:** Real checker against local mock server
- **tests/test_certificates.py:** Certificate tests (expired, invalid, mismatch, expiring)
- **tests/test_runner.py:** Full check runner with database
- **tests/test_api.py:** API endpoints and dashboard
- **tests/test_admin_auth.py:** Admin authentication

### Database Tests
- **tests/test_db.py:** PostgreSQL database operations
- **tests/test_incidents.py:** Incident tracking tests
- **tests/test_db_alerts.py:** Alert log tests

### Security and Validation Tests
- **tests/test_security.py:** SSRF filter tests
- **tests/test_validation.py:** URL validation tests
- **tests/test_admin_sites.py:** Site management tests
- **tests/test_checker.py:** Network stage unit tests
- **tests/test_checker_stages.py:** Individual network stage tests
- **tests/test_health.py:** Health check endpoint

### Mock Failure Server

WhyDown includes a mock failure server (`tests/mock_server.py`) that supports:

- `/ok` - Returns 200 OK quickly
- `/slow?ms=3000` - Delays response
- `/status/500` and `/status/404` - Various HTTP error codes
- `/drop` - Accepts connection and closes without response
- `/hang` - Accepts connection and never responds (read timeout)
- `/keyword-missing` - Returns 200 without expected keyword
- A switch to turn the whole mock site "down"/"up" for incident testing

## Test Case Coverage Matrix

| Test Case | Automated Tests | Manual | Not done |
|-----------|-----------------|--------|----------|
| T01 | `tests/test_checker_integration.py::TestHealthySite::test_healthy_site_reports_ok` | | |
| T02 | `tests/test_checker_integration.py::TestDnsFailure::test_non_existent_domain_is_dns_failure` | | |
| T03 | `tests/test_checker_integration.py::TestTcpFailure::test_closed_port_fails_at_the_tcp_stage` | | |
| T04 | `tests/test_checker_integration.py::TestHang::test_hanging_server_is_read_timeout` | | |
| T05 | `tests/test_checker_integration.py::TestSlowResponse::test_slow_response_is_classified_slow` | | |
| T06 | `tests/test_checker_integration.py::TestHttpErrorStatus::test_http_500_is_server_error` | | |
| T07 | `tests/test_certificates.py::TestT07ExpiredCertificate::test_expired_certificate_is_reported_as_cert_expired` | | |
| T08 | `tests/test_certificates.py::TestT08HostnameMismatch::test_certificate_for_another_host_is_reported_as_cert_invalid` | | |
| T09 | `tests/test_certificates.py::TestT09ExpiringSoon` + `tests/test_cert_alerts.py::test_a_warning_is_sent_when_the_certificate_reaches_a_tier` | | |
| T10 | `tests/test_incidents.py::test_a_single_failure_opens_no_incident` | | |
| T11 | `tests/test_incidents.py::test_exactly_n_failures_open_one_incident` + `tests/test_runner_alerts.py::test_a_down_alert_is_sent_when_an_incident_opens` | | |
| T12 | `tests/test_incidents.py::test_a_failure_while_an_incident_is_open_opens_no_second_one` + `tests/test_alerts.py::test_a_second_down_alert_for_the_same_incident_is_not_sent` | | |
| T13 | `tests/test_incidents.py::test_the_first_up_result_after_an_incident_closes_it` + `tests/test_runner_alerts.py::test_a_recovery_alert_is_sent_when_an_incident_closes` | | |
| T14 | | Manual test on the deployed Render service, 2026-10-25: PASS (see Deployment Tests) | |
| T15 | `tests/test_checker_integration.py::TestSsrfFilter::test_loopback_is_blocked_without_the_test_flag` | | |
| T16 | `tests/test_run_checks.py::test_missing_token_returns_401` | | |
| T17 | `tests/test_validation.py::TestBadSchemes::test_rejected_scheme_is_refused`, `tests/test_validation.py::TestBadSchemes::test_empty_scheme_is_refused`, `tests/test_validation.py::TestCredentials::test_url_with_username_is_rejected`, `tests/test_validation.py::TestCredentials::test_url_with_password_is_rejected`, `tests/test_validation.py::TestMalformedUrls::test_malformed_ipv6_url_is_rejected`, `tests/test_validation.py::TestUrlWhitespace::test_url_with_whitespace_is_rejected`, `tests/test_validation.py::TestUrlWithoutHostname::test_url_without_hostname_is_rejected`, `tests/test_validation.py::TestUrlWithoutHostname::test_url_with_empty_netloc_is_rejected` | | |
| T18 | `tests/load_test.py` (PASS: about 10 to 15 s across runs (measured 10,007 ms and 15,023 ms), budget 25,000 ms) | | |
| T19 | `tests/test_alerts.py::test_a_failing_telegram_call_does_not_raise` + `tests/test_alerts.py::test_a_failing_telegram_call_is_logged_as_a_failed_attempt` | | |
| T20 | `tests/test_checker_integration.py::TestKeywordMissing::test_keyword_missing_is_reported` | | |

## Deployment Tests

Run on 2026-10-25 against the deployed system: a Render free web service, the Supabase database, and a cron-job.org job that calls /run-checks every 10 minutes with the token in the X-Cron-Token header. These are manual tests. The evidence is the Telegram chat, the Incidents page and the cron-job.org history.

### End-to-end test with the scheduler

- The test site render-test (https://example.org) was given the expected status code 404, so every check of it fails as HTTP_4XX.
- Every run in the cron-job.org history screenshots returned 200 OK, in about 3.4 to 5.9 s.
- After 3 consecutive failures one incident was opened for render-test (started 2026-10-25 07:30:50 UTC, failure HTTP_4XX), and one Telegram DOWN alert arrived.
- The expected status code was set back to 200. The next run closed the incident, and one Telegram RECOVERY alert arrived (downtime 4h 49m 44s).
- The incident stayed open for hours because the scheduler was paused between the test windows, to save free instance hours.
- Result: PASS.

### T14: restart in the middle of an incident

- The incident for render-test was open and the DOWN alert had already been sent.
- The service was restarted with Restart service in the Render dashboard (the log shows gunicorn starting again at about 11:50 UTC).
- The scheduler was switched on again and the next two scheduled runs finished with 200 OK.
- No new Telegram message arrived, the Incidents page still showed one incident for render-test with the same start time, and it was still ongoing.
- This shows that the alert state is read from the database and not kept in the memory of the process.
- Not covered: a restart while a check run is in progress.
- Result: PASS.

### Cold start measurement

- Measured on 2026-10-25 at about 06:51 UTC, after the service had been idle overnight with the scheduler paused.
- The first request to /health took 41.7 s; the second took 0.9 s. This is one measurement.

### Scheduler call to a sleeping service

- Observed on 2026-10-26 with cron-job.org: two scheduled runs (9:31 and 9:33 India time) and one test run (10:27) against the sleeping service all ended with Failed (output too large). The response had Content-Type text/html.
- The Render log shows no request from cron-job.org and no app start in that time. The service started only when the site was opened in a browser at 9:38.
- Result: FAIL for a scheduler that is expected to wake the service. Recorded as D-10 (Accepted). The scheduler works while the service is awake (see the end-to-end test).

### Public page watch test (2026-10-28)

Tested on the deployed services: the public page https://whydown-nm.web.app (Firebase, frontend commit `f0f1448`) and the Render service. After a Manual Deploy of the latest commit the Render log showed "Your service is live" (the commit hash was not read from the dashboard). The test site was my own suspended Render site https://risk-lens-hla4.onrender.com, which answers HTTP 503. Two Telegram accounts on one phone were used: the admin account of the bot and a second account acting as a visitor. Times are the phone's local time. The cron-job.org job was switched on for the test and switched off afterwards.

| Step | Expected | Result |
|------|----------|--------|
| 1. Check the suspended site on the page | HTTP_5XX, failed at the http stage, status 503 | PASS (shown as a saved result from 11:39:16, "Already checked") |
| 2. Look for the button | "Alert me on Telegram when it is up" appears for a down site | PASS |
| 3. Start link and "I pressed Start" | the bot opens and the page says Done | PASS |
| 4. Telegram message after confirming | one message "I am watching ..." | PASS (11:42) |
| 5. Site brought back up, scheduler on | exactly one "is up now" message for the visitor | PASS (11:48, HTTP 200, one message) |
| 6. The other (admin) account | no new message | PASS |

Automated tests after this part: 931 passed and `ruff check .` clean (2026-10-28, Wi-Fi on).

Not tested live: the 48-hour expiry, the page for a site that is up (the button should stay hidden), and whether the watch row and the chat id were deleted in the database after the message (the message text says the watch ended; the database was not read).

### Admin security check (2026-10-28)

A read-only check of `app/auth.py` and `app/__init__.py`, plus one request to the deployed login page. Nothing was changed and the login lock-out was not tried live.

| Item | Finding |
|------|---------|
| Password source | read from `ADMIN_PASSWORD` in the environment only; an empty password closes every admin route (fails closed); compared with `hmac.compare_digest` (code read) |
| Local password strength | 12 or more characters with letters and digits (checked by a script that prints only True/False) |
| Render password strength | stated by the author as long and strong; not checked by a script |
| Session cookie flags | `Secure`, `HttpOnly` and `SameSite=Lax` seen in the `Set-Cookie` header of https://whydown.onrender.com/admin/login |
| Session lifetime | 12 hours (`SESSION_LIFETIME` in `app/__init__.py`) |
| Session at login | the session is cleared at login; the CSRF token belongs to the session (code read) |
| Login rate limit | 5 failed logins in a 600 second window lock an address out for one window (code read) |

Known limit (not fixed, recorded as D-12): the lock-out counts failures by `request.remote_addr`. On Render this may be the address of Render's proxy, so all visitors may share one counter, and five wrong logins from anyone could lock the admin out for 10 minutes. The counters are kept in memory, so a restart clears them. How many gunicorn workers run is not confirmed. Not tested live, to avoid locking the admin out before the viva.

## Coverage Report Details

### Per-Module Coverage (actual)

| Module | Lines | Covered | Coverage |
|--------|-------|---------|----------|
| app/checker.py | 280 | 280 | **100%** |
| app/classifier.py | 33 | 33 | **100%** |
| app/security.py | 85 | 79 | **93%** |
| app/incidents.py | 64 | 64 | **100%** |
| app/alerts.py | 289 | 289 | **100%** |
| app/runner.py | 166 | 163 | **98%** |
| app/db.py | 433 | 419 | **97%** |
| app/config.py | 143 | 137 | **96%** |
| app/auth.py | 131 | 126 | **96%** |
| app/routes.py | 611 | 544 | **89%** |
| app/__init__.py | 34 | 34 | **100%** |
| app/validation.py | 89 | 89 | **100%** |
| app/public_check.py | 116 | 111 | **96%** |
| app/public_watch.py | 180 | 170 | **94%** |
| **TOTAL** | **2,654** | **2,538** | **96%** |

### Coverage Targets

- **Overall coverage:** 96% (>=80% target) PASS
- **Classifier coverage:** 100% (project goal, not in spec)
- **Incident logic coverage:** 100% (project goal, not in spec)
- **Critical network stages:** 100% (project goal, not in spec)

### Coverage Gaps (116 statements total)

Missing lines in the run of 2026-10-28 (931 tests, Wi-Fi on). The line descriptions of the earlier list were removed, because the line numbers have moved and were not checked again.

1. **app/auth.py:** 5 statements (lines 117, 181, 225-227)
2. **app/config.py:** 6 statements (lines 78, 209-210, 224, 230, 232)
3. **app/db.py:** 14 statements (lines 224-235 and 250-261; in the earlier list these were the `get_site` function, which no test calls, and the `update_site` query)
4. **app/public_check.py:** 5 statements (lines 51, 62, 64, 158-160)
5. **app/public_watch.py:** 10 statements (lines 57, 63, 66, 94-95, 101-102, 279-281)
6. **app/routes.py:** 67 statements (lines 427-428, 437-438, 451-452, 459, 461, 468, 679, 716-724, 731-739, 766, 950, 955-957, 971-973, 978, 980-982, 985, 997-999, 1019, 1080-1082, 1105-1107, 1119-1121, 1143, 1145-1147, 1159-1161, 1164, 1194-1195, 1218, 1263, 1268)
7. **app/runner.py:** 3 statements (lines 50, 86, 277)
8. **app/security.py:** 6 statements (lines 93, 108, 141-142, 146, 157)

## Test Coverage by Subject

### Subject 1: Cloud Computing
- **Deployment scenarios:** tested manually on the deployed system (see Deployment Tests)
- **Free-tier constraints:** Validated

### Subject 2: Data Communication & Networking
- **Network stages:** DNS, TCP, TLS, HTTP - all covered
- **Failure modes:** DNS_FAILURE, CONNECT_TIMEOUT, TLS_ERROR, READ_TIMEOUT, etc.

### Subject 3: Software Testing & QA
- **Test coverage:** 96%
- **Test quality:** All tests pass
- **Mock server:** Comprehensive failure scenarios


## Test Results Summary

```
============================== 931 passed in 17.37s ===============================
Coverage: 95.00%
Critical modules: checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%; security.py at 93%
19 of 20 spec test cases have an automated test; T14 (alert state persistence across process restart) was tested manually on the deployed service and passed (see Deployment Tests)
```

## Test Quality Metrics

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| Test count | 931 | project goal, not in spec | PASS |
| Pass rate | 100% | project goal, not in spec | PASS |
| Overall coverage | 96% | >=80% | PASS |
| Lowest of security, checker, classifier, incidents, alerts, validation: 93% (security.py) | 93% | project goal, not in spec | n/a (no spec target) |

## Test Verification

The test suite validates:

1. **Network functionality:** DNS, TCP, TLS, HTTP stages work correctly
2. **Failure classification:** Every failure type maps to correct result code
3. **Incident management:** State machine opens/closes incidents correctly
4. **Alert system:** No duplicate alerts, correct timing
5. **Security:** SSRF filter, CSRF protection, token auth
6. **Data integrity:** Database operations, persistence, concurrency
7. **User experience:** Dashboard rendering, API responses
8. **Deployment:** Free-tier constraints, scheduler integration

## Test Automation

All tests run automatically:

```bash
pytest --cov=app --cov-report=html
```

HTML coverage report available at `htmlcov/index.html`

## Conclusions

PASS **All tests pass**
PASS **Coverage meets targets** - 96% overall; critical modules: checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%, security.py at 93%
PASS **Unit and integration tests** - Unit and integration tests exist; the end-to-end test against the deployed system is recorded under Deployment Tests
PASS **T14** - alert state persistence across a process restart was tested manually on the deployed Render service (2026-10-25): no second alert, one incident, same start time
PASS **End-to-end test** - with the deployed scheduler and Telegram (2026-10-25): one DOWN alert after 3 failing runs, one RECOVERY alert, incident closed
PASS **Deployment measurements** - the free-tier deployment on Render works; the cold start was measured once on 2026-10-25 (41.7 s first request, 0.9 s second)
PASS **Public page watch test** - on the deployed page with two Telegram accounts (2026-10-28): the button appeared for a down site, the watch was confirmed, and the visitor got exactly one UP message after the site came back
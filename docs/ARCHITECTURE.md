# WhyDown Architecture

This document shows the components of the WhyDown monitor and the data and control flow between them.

## 1. System overview

```mermaid
flowchart TD
    subgraph ext["External services"]
        X["Supabase Postgres"]
        Y["Telegram"]
        Z["cron-job.org: scheduler (free plan)"]
    end
    A["Browser"] -->|"GET pages"| B["Flask app: app/__init__.py, app/routes.py"]
    B -->|"imports auth and validation"| C["Admin and auth: app/auth.py, app/validation.py"]
    B -->|"imports runner"| D["Runner: app/runner.py"]
    B -->|"imports db"| F["Database layer: app/db.py"]
    B -->|"imports security"| E["Checker: app/checker.py, app/classifier.py, app/security.py"]
    D -->|"calls check_url"| E
    D -->|"calls save_check"| F
    D -->|"calls process_check"| G["Incidents: app/incidents.py"]
    D -->|"calls send_alert"| H["Alerts: app/alerts.py"]
    G -->|"imports db"| F
    H -->|"imports db"| F
    E -->|"DNS, TCP, TLS, HTTP"| I["Monitored websites"]
    F -->|"psycopg connection"| X
    H -->|"Telegram Bot API"| Y
    Z -->|"GET /run-checks"| B
    W["Render: free web service"] -->|"hosts"| B
```

Render and cron-job.org are deployed and were tested on 2026-10-25 (see docs/TEST_PLAN.md).

## 2. One check run

This sequence diagram shows one scheduled run, triggered by the external scheduler.

```mermaid
sequenceDiagram
    participant S as cron-job.org
    participant R as routes.py
    participant N as runner.py
    participant K as checker.py
    participant D as db.py
    participant I as incidents.py
    participant A as alerts.py
    participant T as Telegram

    S->>R: GET /run-checks
    R->>N: run_all_checks
    N->>D: list_sites
    loop for each site
        N->>K: check_url
        K->>K: resolve_dns, connect_tcp, do_tls, do_http
        K->>K: classify_result
        K-->>N: result
        N->>D: save_check
        N->>I: process_check
        N->>A: send_alert when an incident opens or closes
        A->>T: Telegram message
    end
    N->>D: purge_old_checks
    N-->>R: run summary
    R-->>S: JSON answer
```

Function names are taken from the code.

## 3. Database

This diagram shows the tables defined in docs/init_db.sql.

```mermaid
erDiagram
    sites {
        int id PK
        text name
        text url
        int expected_status
        text keyword
        int slow_threshold_ms
        boolean enabled
        timestamp created_at
    }
    checks {
        bigint id PK
        int site_id FK
        timestamp checked_at
        int status_code
        text result
        text failed_stage
        int dns_ms
        int tcp_ms
        int tls_ms
        int ttfb_ms
        int total_ms
        text resolved_ip
        timestamp cert_expires_at
        boolean cert_expiring_soon
        text error_detail
    }
    incidents {
        int id PK
        int site_id FK
        timestamp started_at
        timestamp ended_at
        text failure_type
        int consecutive_failures
    }
    alert_log {
        int id PK
        int incident_id FK
        int site_id FK
        text kind
        timestamp sent_at
        text channel
        boolean success
        timestamp cert_expires_at
        int cert_threshold_days
    }
    sites ||--o{ checks : "has"
    sites ||--o{ incidents : "opens"
    sites ||--o{ alert_log : "has"
    incidents |o--o{ alert_log : "logs"
```

Source: docs/init_db.sql. Column types are simplified.

## 4. Source map

| Component | File | Role |
|---|---|---|
| Browser | none | Reads the dashboard, incidents and admin pages served by the Flask app |
| Flask app | app/__init__.py, app/routes.py | Creates the application; serves pages, the JSON API and /run-checks |
| Admin and auth | app/auth.py, app/validation.py | Admin login with CSRF tokens and a login rate limit; validation of site input |
| Runner | app/runner.py | Runs all site checks with a per-site timeout and a run deadline, then saves the results |
| Checker | app/checker.py, app/classifier.py, app/security.py | check_url does DNS, TCP, TLS and HTTP; classify_result sets the result code; security blocks private targets |
| Database layer | app/db.py | All SQL, through psycopg |
| Incidents | app/incidents.py | process_check opens and closes incidents from check results |
| Alerts | app/alerts.py | Sends Telegram DOWN, RECOVERY and certificate-expiry alerts and logs them in alert_log |
| Monitored websites | none | Sites stored in the sites table and checked over the network |
| Supabase Postgres | docs/init_db.sql | Hosted Postgres database; this file holds the schema |
| Telegram | app/alerts.py | Receives alert messages through the Bot API |
| cron-job.org | none | External scheduler (free plan) that calls /run-checks with the X-Cron-Token header |
| Render | render.yaml | Hosting on a free web service; render.yaml defines the service |
| Configuration (not drawn in section 1) | app/config.py | Settings such as the runner timeouts |

## 5. Free-plan limits and operation

Checked on 2026-10-25 in the documentation of each provider.

| Service | Limit | Effect on WhyDown |
|---------|-------|-------------------|
| Render free web service | Spins down after 15 minutes without inbound traffic; the next request wakes it (the docs say about one minute; 41.7 s was measured once) | The first request after a sleep is slow |
| Render free web service | 750 free instance hours per workspace per month; a spun-down service uses none; when they run out, free web services are suspended until the next month | A scheduler that calls every 10 minutes keeps the service awake and uses about 720 to 744 hours of a 31-day month, so it cannot run all month with a safety margin |
| Render free web service | May be suspended for an uncommonly high volume of traffic started by the service; no number is given | Not confirmed for WhyDown; keep the number of monitored sites small |
| Render free web service | May be restarted at any time | The incident state is kept in the database (T14) |
| cron-job.org | Free; at most one run per minute; request timeout 30 s; response read up to 64 KB; no limit on jobs per account under fair use | A call to a sleeping service fails and does not wake it (D-10), so the scheduler works only while the service is awake |
| Supabase free | 500 MB database; the project is paused when user database activity is too low over a week (a few requests a day typically prevents it); a paused project can be restored from the dashboard within 90 days; no automatic backups | The database gets a small visit by hand every 5 days |

### Operation

- During development and tests the cron-job.org job (every 10 minutes) is switched on only for short test windows and switched off afterwards, to save free instance hours.
- The 750 hours reset every month, so the hours that matter are those of the month of the viva.
- The scheduler cannot wake a sleeping service (D-10). The service is woken by opening the site in a browser, and the cron-job.org job is switched on only after that.
- To keep the Supabase project from pausing, the author opens the dashboard and adds and removes a small test site in the admin page every 5 days by hand (see docs/FREE_TIER_CHECKLIST.md).

## Not confirmed

- Section 2 shows only the scheduled check run. It does not show certificate-expiry alerts (send_cert_alert is imported at app/runner.py line 33) or dashboard page requests.
- No arrow is drawn from Admin and auth to the database layer, because no import of it was found in app/auth.py or app/validation.py.
- The render.yaml startCommand (app:create_app) runs with gunicorn on Render (deploy log of 2026-10-25).
- The volume of outgoing traffic at which Render suspends a free web service is not known: the Render documentation gives no number. Not confirmed for WhyDown.
- Why Render answers cron-job.org with an HTML page and does not wake the service is not confirmed (D-10). The meaning of x-render-routing: no-deploy was not found in the Render documentation.
- Whether a visit every 5 days is enough to keep the Supabase project from pausing is not confirmed; the Supabase documentation says a few requests a day typically prevent it.
- Section 3 simplifies the column types and does not draw the indexes in docs/init_db.sql.

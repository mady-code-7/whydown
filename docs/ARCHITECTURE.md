# WhyDown Architecture

This document shows the components of the WhyDown monitor and the data and control flow between them.

## 1. System overview

```mermaid
flowchart TD
    subgraph ext["External services"]
        X["Supabase Postgres"]
        Y["Telegram"]
        Z["cron-job.org: planned, not deployed yet"]
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
    W["Render: planned, not deployed yet"] -->|"hosts"| B
```

Boxes marked planned are not deployed yet.

## 2. One check run

This sequence diagram shows one scheduled run, triggered by the planned external scheduler.

```mermaid
sequenceDiagram
    participant S as cron-job.org planned
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

Function names are taken from the code; the scheduler is planned, not deployed yet.

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

<!-- NEXT -->

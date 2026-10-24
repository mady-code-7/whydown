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

<!-- NEXT -->

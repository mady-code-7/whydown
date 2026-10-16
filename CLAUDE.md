# WhyDown

Website uptime + network diagnostics monitor. Full spec: @docs/SPEC.md

## Hard rules
- Strictly free tiers only. Never add a paid or pay-as-you-go service.
- No secrets in the repo. Config comes from environment variables (see .env.example).
- Never store response bodies, only metadata.

## Stack
Python 3, Flask, socket/ssl/http.client, Postgres (psycopg or SQLAlchemy),
Jinja2 + Chart.js, pytest + pytest-cov, ruff.

## Conventions
- Networking logic lives in small pure functions (resolve_dns, connect_tcp,
  do_tls, do_http, classify) so they can be unit-tested with mocks.
- Time with time.perf_counter. Every error must be caught and classified;
  one site failing must never stop a run.
- Parameterised SQL only. Block private/loopback IPs after DNS resolution (SSRF).
- Use the repo structure in section 16 of the spec.

## Commands
- Run tests: pytest --cov=app
- Lint: ruff check .
- Run locally: flask --app app run

## Workflow
Build in the phases from section 18 of the spec, one phase at a time.
Write tests alongside code, not after.

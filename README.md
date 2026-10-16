# WhyDown

**Not just *if* a website is down, but *why*.**

WhyDown is a website uptime and network diagnostics monitor. Register the sites you care about, and WhyDown checks them on a schedule, breaking each request into network stages: DNS lookup, TCP connect, TLS handshake, time to first byte and total response time.

When a site fails or slows down, WhyDown classifies the problem (for example a DNS failure, connection refused, timeout, certificate error or HTTP error), opens an incident and sends an alert. A dashboard shows uptime, response-time history and a per-stage timing breakdown.

## Features

- Monitors multiple HTTP/HTTPS sites on a fixed schedule
- Per-stage timing: DNS, TCP, TLS, TTFB and total
- Failure classification that points to where in the network path things broke
- Incident tracking with a single down alert and a single recovery alert
- Certificate expiry warnings
- Dashboard with uptime percentages, response-time charts and incident history
- Telegram or email alerts
- Built-in protection against server-side request forgery (SSRF)
- Mock failure server and automated tests with a coverage report

## Tech Stack

- **Backend:** Python, Flask
- **Database:** Free hosted Postgres
- **Frontend:** Jinja2, HTML/CSS, Chart.js
- **Hosting:** Render (free tier) with an external free scheduler
- **Testing:** pytest, pytest-cov

Everything runs on free tiers only, with no pay-as-you-go services.

## Getting Started

```bash
git clone <repository-url>
cd whydown

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # fill in your own values
flask --app app run --debug
```

Configuration (database connection, secrets and alert credentials) is read from environment variables and is never committed to the repository.

## Testing

```bash
pytest
pytest --cov=app
```

## Project Structure

```
whydown/
├── app/        # Flask app, checker, classifier, incidents, alerts
├── tests/      # Unit, integration and API tests, plus the mock server
├── docs/       # Test plan, report and screenshots
└── README.md
```

## Limitations

- Checks run from a single location, so "down" can mean "down from here"
- HTTP/HTTPS checks only
- Checks run every few minutes, not every few seconds

## Contributing

Fork the repository, create a branch, add tests for your changes, and open a pull request.

## License

Add a license of your choice.
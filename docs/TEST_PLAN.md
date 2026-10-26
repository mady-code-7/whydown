# WhyDown Test Plan

## Executive Summary
- **Tests passed:** 794
- **Tests failed:** 0
- **Tests skipped:** 0
- **Overall test coverage:** 95% (2,241 statements total, 102 missing)
- **Critical modules tested:** checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%; security.py at 92%
- **Missing coverage:** 102 statements (4.6% total)

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
- **app/security.py:** SSRF filter (92% coverage, 6 missing statements)
- **app/incidents.py:** Incident state machine (100% coverage)
- **app/alerts.py:** Telegram alert sending (100% coverage)
- **app/runner.py:** Full check runner (98% coverage)
- **app/db.py:** PostgreSQL access layer (96% coverage)
- **app/config.py:** Environment configuration (95% coverage)
- **app/auth.py:** Admin authentication (95% coverage)
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

## Coverage Report Details

### Per-Module Coverage (actual)

| Module | Lines | Covered | Coverage |
|--------|-------|---------|----------|
| app/checker.py | 280 | 280 | **100%** |
| app/classifier.py | 33 | 33 | **100%** |
| app/security.py | 76 | 70 | **92%** |
| app/incidents.py | 64 | 64 | **100%** |
| app/alerts.py | 289 | 289 | **100%** |
| app/runner.py | 166 | 163 | **98%** |
| app/db.py | 348 | 334 | **96%** |
| app/config.py | 131 | 125 | **95%** |
| app/auth.py | 131 | 125 | **95%** |
| app/routes.py | 600 | 533 | **89%** |
| app/__init__.py | 34 | 34 | **100%** |
| app/validation.py | 89 | 89 | **100%** |
| **TOTAL** | **2,241** | **2,139** | **95%** |

### Coverage Targets

- **Overall coverage:** 95% (>=80% target) PASS
- **Classifier coverage:** 100% (project goal, not in spec)
- **Incident logic coverage:** 100% (project goal, not in spec)
- **Critical network stages:** 100% (project goal, not in spec)

### Coverage Gaps (102 statements total)

1. **app/security.py:** 6 statements (lines 91 default resolver, 106 DNS timeout, 139-140 URL parse error, 144 missing hostname, 155 public IP literal return)
2. **app/db.py:** 14 statements (lines 224-235 entire `get_site` function - no test calls `get_site` at all, lines 250-261 `update_site` query and execution)
3. **app/config.py:** 6 statements (line 78 empty part skip, lines 196-197 invalid URL, 211 DB timeout <=0, 217 site timeout <1, 219 run deadline <1)
4. **app/auth.py:** 6 statements (line 117 no configured password, 181 JSON body CSRF token, 225-227 login lock expiry, 281 non-API not-configured redirect)
5. **app/routes.py:** 67 statements (lines 425-426 `_row_id` exception, 435-436 `_int_value` exception, 449-450 `_number` exception, 457/466 `_chart_label`/`_date_text` naive datetime, 459 `_chart_label` fallback, 677 `_incident_sort_key` naive datetime, 714-722 `_as_int` string parsing, 729-737 `_as_bool` parsing, 764 `_safe_next` fallback, 939 `api_add_site` non-dict JSON, 944-946/969-971/986-988/1069-1071/1094-1096/1108-1110/1134-1136/1148-1150 DB exception handlers, 960-962/1132 SSRF on URL change, 967/1132 site limit error, 974/1153 not found, 1008 already logged in, 1183-1184 `_admin_page_error` DB fallback, 1207 `_coerce_form_values` enabled coercion, 1252/1257 `_validated_site_values` enabled flag)
6. **app/runner.py:** 3 statements (line 50 `_close_connection` None conn, 86 `_RunConnection.reopen` already broken, 277 `_run_sites` run deadline break)

## Test Coverage by Subject

### Subject 1: Cloud Computing
- **Deployment scenarios:** not tested yet (deployment is Phase 7)
- **Free-tier constraints:** Validated

### Subject 2: Data Communication & Networking
- **Network stages:** DNS, TCP, TLS, HTTP - all covered
- **Failure modes:** DNS_FAILURE, CONNECT_TIMEOUT, TLS_ERROR, READ_TIMEOUT, etc.

### Subject 3: Software Testing & QA
- **Test coverage:** 95%
- **Test quality:** All tests pass
- **Mock server:** Comprehensive failure scenarios


## Test Results Summary

```
============================== 794 passed in 14.44s ===============================
Coverage: 95.00%
Critical modules: checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%; security.py at 92%
19 of 20 spec test cases have an automated test; T14 (alert state persistence across process restart) was tested manually on the deployed service and passed (see Deployment Tests)
```

## Test Quality Metrics

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| Test count | 794 | project goal, not in spec | PASS |
| Pass rate | 100% | project goal, not in spec | PASS |
| Overall coverage | 95% | >=80% | PASS |
| Lowest of security, checker, classifier, incidents, alerts, validation: 92% (security.py) | 92% | project goal, not in spec | n/a (no spec target) |

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
PASS **Coverage meets targets** - 95% overall; critical modules: checker.py, classifier.py, incidents.py, alerts.py and validation.py at 100%, security.py at 92%
PASS **Unit and integration tests** - Unit and integration tests exist; the end-to-end test against the deployed system is recorded under Deployment Tests
PASS **T14** - alert state persistence across a process restart was tested manually on the deployed Render service (2026-10-25): no second alert, one incident, same start time
PASS **End-to-end test** - with the deployed scheduler and Telegram (2026-10-25): one DOWN alert after 3 failing runs, one RECOVERY alert, incident closed
PASS **Deployment measurements** - the free-tier deployment on Render works; the cold start was measured once on 2026-10-25 (41.7 s first request, 0.9 s second)
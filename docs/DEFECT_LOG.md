# WhyDown Defect Log
This file contains the WhyDown defect log, documenting confirmed issues and their status.

## Defects

| ID | Found in (phase) | Description | Severity | How found | Fix (commit hash or file) | Status |
|----|------|-------------|----------|-----------|--------------------------|--------|
| D-01 | Phase 1-2 (setup) | Plain pytest could not find the app package without pythonpath configuration in pytest.ini | Medium | Plain pytest run could not import app | 39dd573 | Fixed |
| D-02 | Phase 2 | On Windows, connections to closed loopback ports are silently dropped (CONNECT_TIMEOUT) instead of refused (CONNECTION_REFUSED). Linux production is not affected. | Low | Test run on the author's Windows machine | No code fix. test_closed_port_fails_at_the_tcp_stage (tests/test_checker_integration.py) accepts either result; test_connection_refused_is_classified_as_refused (tests/test_checker.py) covers the refused mapping. | Accepted |
| D-03 | Phase 5 | The commit subject says Phase 8 but the work belongs to Phase 5; ADMIN_PASSWORD=admin and SECRET_KEY=change-me-in-production changed to empty strings | High | Review of .env.example | 8cd7a28; test_an_empty_admin_password_refuses_every_write_route (tests/test_admin_auth.py) | Fixed |
| D-04 | Phase 5 | app/templates/admin_sites.html ignored the values variable, so a failed add or edit did not show the typed values again. | Medium | Author's note during Phase 5 | 989953f; test_failed_edit_shows_the_typed_values and test_script_is_escaped_in_a_field_value (tests/test_admin_sites.py) | Fixed |
| D-05 | Phase 3 | Before this fix a database connection was not reused within a run; the subject of commit aa94b08 is: reuse one database connection per run. | Medium | Author's observation of slow runs against the real database | aa94b08 | Fixed |
| D-06 | Phase 2 | app/checker.py line 136: addrinfo = socket.getaddrinfo( is called with no timeout argument. The per-site timeout in app/runner.py (line 268) bounds the whole check from outside; the lookup call itself has no timeout. The comment on line 135 mentions a timeout but none is applied. | Medium | Code read during 6B-2 | None yet | Open |
| D-07 | Phase 2 | app/checker.py line 139: socket.AF_INET, is passed to getaddrinfo, so only IPv4 addresses are resolved. Line 178 also creates the socket with socket.AF_INET. | Medium | Code read during 6B-2 | None yet | Open |
| D-08 | Phase 5 | app/validation.py line 52: def _reject_schemes( is defined. No code in app/ calls it. Only tests call it: tests/test_validation.py lines 321, 325, 330, 331. | Low | Code read during 6B-2 | None yet | Open |
| D-09 | Phase 6 | In the load test all 5 hanging sites are recorded as CONNECT_TIMEOUT with failed_stage tcp (tests/load_test.py lines 39 and 133). The cause is the per-site timeout of 10 seconds (app/config.py line 174, enforced at app/runner.py line 268): it ends the check before the checker read timeout of 15 seconds (app/checker.py line 84) can apply, and _record_timeout (app/runner.py lines 155-166) stores TIMEOUT_RESULT (line 44, value CONNECT_TIMEOUT). Spec T04 expects READ_TIMEOUT (docs/SPEC.md line 287), and check_url alone does return READ_TIMEOUT for a server that never replies when short test timeouts are used (test_hanging_server_is_read_timeout, tests/test_checker_integration.py line 210). | Medium | Load test output and code read during 6B-3 | None yet | Open |
| D-10 | Phase 7 | The first cron-job.org test run after the Render free web service had spun down failed with Failed (output too large). The same call with the service awake returned 200 OK (57 bytes). The cold start measured on 2026-10-25 was 41.7 s, longer than the 30 s timeout of cron-job.org, so a scheduled run that reaches a sleeping service can fail. The cause of the message is not confirmed (see Could not confirm). | Medium | cron-job.org test run (7D) and cold start measurement (7E) | No code fix. Handled by how the scheduler is operated (see the section Free-plan limits and operation in docs/ARCHITECTURE.md) | Accepted |
| D-11 | Phase 1 | app/config.py line 235 uses a default DATABASE_URL with localhost and a dummy password (postgres:postgres), and .env.example has similar placeholders. They are placeholders, not real credentials, but they are visible in the public repository. | Low | Secret check before the repository was made public (Phase 7) | None. The real value is set in the DATABASE_URL environment variable on Render | Accepted |

## Severity
This is the author's assessment of severity levels.
High = wrong results or a security risk
Medium = a failure in testing or a wrong user-facing behaviour
Low = cosmetic or tidy-up

## Could not confirm
An unreachable scheme check block in app/validation.py said to be removed in e9bbf21: not provable, because commit e9bbf21 created the file (193 new lines), so no earlier version exists in git.

The cause of the Failed (output too large) result of the first cron-job.org test run after the Render service had spun down (D-10): the response of that run was not captured. It happened only on the first request after the service had slept, so the wake-up is the likely cause, but this is not proven. The repeat run with the service awake returned 200 OK with 57 bytes.

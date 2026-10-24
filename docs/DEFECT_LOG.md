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

## Severity
This is the author's assessment of severity levels.
High = wrong results or a security risk
Medium = a failure in testing or a wrong user-facing behaviour
Low = cosmetic or tidy-up

## Could not confirm
An unreachable scheme check block in app/validation.py said to be removed in e9bbf21: not provable, because commit e9bbf21 created the file (193 new lines), so no earlier version exists in git.
# WhyDown Public Check: Design (Phase 8D)

Status: draft, written 2026-10-26, before any code. Deadline: 2026-10-30.

## 1. Goal

Anyone on the internet opens a public page, pastes a website address and sees the result as a clear page: DNS, TCP, TLS, TTFB, total time, and why it failed. If the site is down, an optional button "Alert me when it is up" appears. The user then gets ONE Telegram message when the site is up again. Nothing else is sent to public users.

## 2. Split of services (all free tier)

- Public page: Firebase Hosting (Spark plan, static files only), folder `frontend/` (HTML, CSS, JavaScript). No Cloud Functions, no Cloud Run, no card.
- Backend: the existing Flask app on Render (free web service). Gets new public endpoints and keeps the admin pages.
- Database: Supabase free Postgres (rate limit, saved results, watches).
- Scheduler: cron-job.org (checks the watches together with the admin sites).
- Alerts: the existing Telegram bot @whydown_nm_bot. Owner alerts and public alerts use the same bot, with different chat IDs.

## 3. Render becomes admin only

- `/` and the read-only JSON API need the admin login. `/health` stays public. `/run-checks` stays protected by the token header.
- Public users never need the Render address. Only the page code knows it (it is not a secret).

## 4. Public endpoints on Render (names are proposals)

- `POST /api/public/check` with a URL. Returns the result and a watch offer when the site is down.
- `POST /api/public/watch` creates a watch and returns a Telegram link `https://t.me/whydown_nm_bot?start=<code>`.
- `GET /api/public/watch/<id>` lets the page see if Telegram is connected.
- `POST /telegram/webhook` receives the Start message. It checks the secret header set by Telegram. The secret is a new value that I type into Render myself.
- CORS: only the Firebase address is allowed (value in an environment variable). Written by hand, no new pip package.
- No secret is ever placed in `frontend/`.

## 5. Rules

- Check: one check per hour per visitor (identified by IP), any URL. The same URL again inside the hour returns the saved result with the note "Already checked. Result from <time>." No new check is run.
- Demo switch: environment variable `PUBLIC_RATE_LIMIT_ON`. Set to false only on demo day so several sites can be shown in a row.
- IP is stored only as a keyed hash, never as plain text. Rows older than 7 days are deleted.
- Same safety rules as now: SSRF filter after DNS resolution, only http and https, response body read at most 1 MB and never stored, parameterised SQL only, certificate verification stays on.
- Watch: the button appears only when the site is down. The page shows: "The watch lasts 48 hours. After that we will not alert you."
- A watch ends at the first OK check (one UP message) or after 48 hours. No DOWN message, no reminders, no certificate warnings for public users.
- One watch per Telegram account and URL. At most 10 active public watches in total (the run deadline is 25 s; raise only after the load test).
- The Telegram chat ID is deleted when the watch ends.
- UP message: one plain-text line, for example "example.com is up again."

## 6. Data (proposed tables)

- `public_checks`: visitor hash, normalised URL, result summary, checked_at.
- `public_watches`: id, URL, link code, chat ID (empty until Start), status, created_at, expires_at.
- Added to `docs/init_db.sql` with CREATE TABLE IF NOT EXISTS.

## 7. Build order (each part ends with tests and one commit)

1. Backend: public check, rate limit, saved result, CORS, tests.
2. Frontend: `frontend/` page, Firebase new site, deploy.
3. Admin lockdown of `/` and the JSON API, tests.
4. Watches, Telegram Start link and webhook, cron processing, tests.
5. End-to-end test on the deployed services, record in `docs/TEST_PLAN.md`.
6. Docs and report. If part 4 slips, it is cut and the report does not mention it.

Dates: 26 Oct design, 27 Oct part 1, 28 Oct parts 2 and 3, 29 Oct part 4 and 5, 30 Oct buffer and report.

## 8. Not confirmed (risks)

- Whether Telegram can deliver the webhook to a sleeping Render service (cron-job.org could not wake it, defect D-10). The user has just used the page, so the service should be awake; not tested.
- How Render passes the visitor IP in the forwarding header, and whether a visitor can fake it. To be checked on the deployed service before the limit is trusted.
- Cold start of about 40 s on the first request after sleep (one measurement). The page must show "Server is waking up".
- With the cron job ON all month, one always-on service uses about 720 to 744 of the 750 free hours. Sleep and wake behaviour is a guess until measured.
- Firebase Hosting free quota: sources disagree, not confirmed.
- 10 watches plus the admin sites inside the 25 s run deadline: not tested.

## 9. Out of scope

SMS or phone numbers (Telegram cannot message a number), user accounts, DOWN messages for public users, paid services.

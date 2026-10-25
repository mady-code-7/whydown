# Free-tier checklist

Written on 2026-10-25. WhyDown runs on free plans: Render (web service), Supabase (database) and cron-job.org (scheduler). This list keeps them from being paused or suspended. The project is expected to stay live for 2 to 3 months.

## What can cost a free service

- Render: if the 750 free instance hours of a month are used up, free web services are suspended until the next month. A service that sleeps uses no hours.
- Supabase: a free project is paused when it gets too little database activity over a week. A few requests a day typically prevent this. A paused project can be restored with Resume project within 90 days.
- cron-job.org: free, no card needed. A job can be disabled automatically after too many failures (the number is not known).

## Every month (about 5 minutes)

1. Render dashboard, Billing, Monthly Included Usage: look at the free instance hours and the outbound bandwidth. If more than 600 hours are used, switch off any 10-minute job in cron-job.org.
2. Supabase dashboard: the project must not show Paused. If it does, click Resume project.
3. cron-job.org, Cronjobs list: both daily jobs must be enabled. Open the History of whydown run-checks: the latest runs must be 200 OK. If a job is disabled, edit it and enable it again.
4. Open the WhyDown dashboard: every site must show a check from the last day.

## Two days before the viva

1. Do the monthly checks above.
2. In the Render Billing page, check that enough free hours are left for the viva day.
3. Delete the test site render-test and add the demo sites (a few big live sites and one site that is down).
4. Test one Telegram alert.

## On the viva day

1. Open the WhyDown URL 10 minutes early. A sleeping service needs up to a minute to wake.
2. For live checks during the demo: in cron-job.org, edit whydown run-checks and set Every 10 minutes. Change it back to Every day at 4:03 afterwards.

## If something goes wrong

- Supabase shows Paused: click Resume project (possible within 90 days of the pause).
- Render shows the service as suspended: free services stay suspended until the next month starts. Plan B: run the app on the laptop with the same Supabase database.
- A cron-job.org job is disabled: edit it, turn Enable job on, save.

# Free-tier checklist

Updated on 2026-10-26. WhyDown runs on free plans: Render (web service), Supabase (database) and cron-job.org (scheduler). This list keeps them from being paused or suspended. The project is expected to stay live for 2 to 3 months.

## What can cost a free service

- Render: if the 750 free instance hours of a month are used up, free web services are suspended until the next month. A service that sleeps uses no hours. A sleeping service is not lost.
- Supabase: a free project is paused when it gets too little database activity over a week. A few requests a day typically prevent this. A paused project can be restored with Resume project within 90 days.
- cron-job.org: free, no card needed. A scheduled call does not wake a sleeping service (D-10), so it is used only while the service is awake. A job can be disabled automatically after too many failures (the number is not known).

## Every 5 days (phone alarm at 10:30 PM, about 2 minutes)

1. Open https://whydown.onrender.com/ and wait up to a minute for the page. This wakes the service and reads from the database.
2. Open /admin/login and log in. Add a site named keepalive with the URL https://example.com, then remove it, then log out. This writes to the database.
3. Close the page. The service falls asleep by itself about 15 minutes later.

## Every month (about 5 minutes)

1. Render dashboard, Billing, Monthly Included Usage: look at the free instance hours and the outbound bandwidth.
2. Supabase dashboard: the project must say Active, not Paused. If it says Paused, click Resume project. Look for a warning email from Supabase.
3. cron-job.org, Cronjobs list: whydown run-checks must exist and be switched off (Inactive).
4. Open the WhyDown dashboard and check that it loads.

## Two days before the viva

1. Do the monthly checks above.
2. In the Render Billing page, check that enough free hours are left for the viva day.
3. Delete the test site render-test and add the demo sites (a few big live sites and one site that is down).
4. Test one Telegram alert.

## On the viva day

1. Open the WhyDown URL 10 minutes early and wait until the page loads. The service must be awake before the scheduler is switched on.
2. For live checks during the demo: in cron-job.org, edit whydown run-checks, check that the schedule is Every 10 minutes, turn Enable job on and save.
3. After the demo, turn Enable job off again.

## If something goes wrong

- Supabase shows Paused: click Resume project (possible within 90 days of the pause).
- Render shows the service as suspended: free services stay suspended until the next month starts. Plan B: run the app on the laptop with the same Supabase database.
- The scheduler job shows failures: the service is probably asleep. Open the site in a browser first, wait for the page, then run the job again.

# Deploying DocTruth AI from scratch

Everything runs on free tiers: Vercel Hobby (API, dashboard, daily cron), Supabase Postgres (data and job queue), Groq (LLM, optional).

## 1. Database (Supabase)
Create a project. Use a short password with letters and numbers only. Copy the Transaction pooler connection string and turn it into `postgresql+asyncpg://postgres.<project-ref>:<password>@<host>:6543/postgres`. Create the tables from the `backend` folder with `alembic upgrade head`.

## 2. GitHub App
Create a GitHub App with the webhook URL `https://<your-app>.vercel.app/webhooks/github` and a webhook secret. Repository permissions: Contents read and write, Pull requests read and write, Metadata read. Subscribe to the events Pull request, Push and Installation. Generate a private key. Install the app on the repositories you want.

## 3. Vercel
Import the repository, set the root directory to `backend`, and add these environment variables.

| Name | Meaning |
| --- | --- |
| DATABASE_URL | the asyncpg string from step 1 (sensitive) |
| GITHUB_APP_ID | numeric app id |
| GITHUB_APP_PRIVATE_KEY | whole PEM text (sensitive) |
| GITHUB_WEBHOOK_SECRET | same secret as in the app |
| CRON_SECRET | long random string that protects the job runner |
| PUBLIC_BASE_URL | `https://<your-app>.vercel.app` |
| GROQ_API_KEY | optional; without it DocTruth uses rules and asks questions |
| DASHBOARD_TOKEN | long random password for the dashboard |
| DRY_RUN | optional; true means analyse and store, write nothing to GitHub |

Redeploy after changing a variable. Check `/health`, then push a change and watch a new row appear in the dashboard.

## 4. Rotating secrets
Database password: reset it in Supabase, update DATABASE_URL, redeploy. GitHub private key: generate a new key, update GITHUB_APP_PRIVATE_KEY, redeploy, test, then delete the old key in the app settings.

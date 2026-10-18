# Decisions

- **Free online hosting, no card**: Vercel (API) + Supabase (Postgres + pgvector) + Groq (LLM later). Nothing runs locally.
- **No Redis / separate worker**: Vercel can't run long-lived workers. The job queue is a Postgres table
  (`FOR UPDATE SKIP LOCKED`, retries with backoff, dead-letter status). After a webhook the API calls its own
  `/internal/jobs/run` so work starts immediately; a daily Vercel cron is the safety net.
- **Webhook dedupe**: by delivery ID (replays) and by job `dedupe_key` (a merged PR also fires a push with the same SHA -> one job).
- **Own doc-sync PRs ignored**: branches starting with `doctruth/` never trigger runs (no loops).
- **docker-compose / Dockerfiles skipped** (spec asks for them, but deployment is Vercel).

# DocTruth AI
The default timeout is 30s.
Call `fetch(url, timeout=30)` to download.
Keeps docs true: reads each merged PR (code + discussion + history), finds doc claims that became false, opens a doc-fix PR with evidence.
Phase 1 = foundation: GitHub App webhook receiver, Postgres job queue, DB schema, run state machine.
Everything runs online (Vercel + Supabase), nothing local except one migration command.
See `docs/DECISIONS.md`.

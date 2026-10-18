# Decisions

- **Free online hosting, no card**: Vercel (API) + Supabase (Postgres + pgvector) + Groq (LLM later). Nothing runs locally.
- **No Redis / separate worker**: Vercel can't run long-lived workers. The job queue is a Postgres table
  (`FOR UPDATE SKIP LOCKED`, retries with backoff, dead-letter status). After a webhook the API calls its own
  `/internal/jobs/run` so work starts immediately; a daily Vercel cron is the safety net.
- **Webhook dedupe**: by delivery ID (replays) and by job `dedupe_key` (a merged PR also fires a push with the same SHA -> one job).
- **Own doc-sync PRs ignored**: branches starting with `doctruth/` never trigger runs (no loops).
- **docker-compose / Dockerfiles skipped** (spec asks for them, but deployment is Vercel).

## Phase 2: evidence collectors

- **Collectors are pure functions** (`app/evidence/*_collector.py`): they take already-fetched data and return plain objects. All GitHub I/O lives in `evidence/collect.py` + `github/client.py`, so the parsing logic is tested with fixtures and no network.
- **Symbol-level diff, not line diff.** tree-sitter extracts functions, methods, classes, constants, routes, env vars and CLI flags (Python, JS, TS/TSX). Changes: added, removed, renamed (same body hash), signature_changed, default_changed, value_changed, body_changed, file_renamed. Private (`_name`) symbols are ignored.
- **Fusion rules in code** (`evidence/graph.py`): a PR/commit/resolved-thread statement about a default that differs from the merged code becomes a `contradicts` edge (code wins on behavior). Unresolved threads only produce `mentions` edges and are never used as facts. Reverts produce `supersedes` edges.
- **No schema change**: edges carry no payload, so contradiction details (claimed vs. code value) are stored on the symbol node under `contradicted_by`.
- **Run outcomes in this phase**: `SKIPPED` (internal_only) when no JS/TS/Python file changed or no public symbol changed; otherwise `EVIDENCE_COLLECTED`. A job that exhausts its retries leaves a `FAILED` run.
- **Limits** (bounded work per serverless call): 30 source files per run, files over 200 KB skipped, 50 review threads, 5 linked issues.
- **Not done yet (see ROADMAP in Phase 10)**: `git blame` per changed line and "previous related PRs" for the history collector; doc_section/claim nodes arrive in Phase 3.

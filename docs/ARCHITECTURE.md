# DocTruth AI: how it works

DocTruth reads each pull request, finds documentation claims that the code change made false, and fixes them with evidence. It never guesses: when it is not sure it asks.

## Flow

1. GitHub sends a webhook to `/webhooks/github` (signature verified, delivery id deduplicated). An open or updated pull request becomes a preview job. A merged change becomes a sync job. Branches named `doctruth/*` are ignored.
2. A Postgres job queue (no Redis) holds the job. The API triggers `/internal/jobs/run` right away; a daily cron is the safety net. Failed jobs retry with backoff and finally become dead jobs with a FAILED run.
3. The run moves through a state machine: RECEIVED, SCOPED, EVIDENCE_COLLECTED, ANALYZED, DRAFTED, VERIFIED, PUBLISHED (or SKIPPED, NEEDS_HUMAN, FAILED). Every step is stored with a note.
4. Evidence: tree-sitter reads Python, JavaScript and TypeScript and compares symbols before and after. Pull request text, review threads and commit history are fused into an evidence graph. Code beats discussion on behaviour. Resolved threads are decisions, unresolved threads are open questions, never facts.
5. Claims: Markdown in the root and in `docs/` is split into atomic claims (signature, default value, CLI flag, path, link, version, code example, behaviour statement). Deterministic checks give each a verdict.
6. Edits: rules fix what can be fixed mechanically. The LLM (Groq) only judges prose and rewrites examples. Every edit must cite evidence, stay close to the original, invent no symbol or path, and pass a critic. Anything below the confidence threshold becomes a question.
7. Publishing: a preview comment on an open pull request, or a doc-sync pull request on `doctruth/sync-<n>` after merge. Both are idempotent.

## Where things live

- `backend/app/api`: webhook, job runner, dashboard.
- `backend/app/github`: app authentication and the REST/GraphQL client.
- `backend/app/evidence`: code, discussion and history collectors and the graph.
- `backend/app/claims`: claim extraction and drift checks.
- `backend/app/pipeline`: run driver, rewrite rules, reasoning, publishing.
- `backend/app/llm`: LLM client, prompts, scrubber, schemas.
- `backend/app/eval`: the offline benchmark.
- `docs/DECISIONS.md`: why each phase was built the way it was.

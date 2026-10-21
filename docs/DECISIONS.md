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

## Phase 3: claims and deterministic drift

- **Claims are extracted with rules, not an LLM** (`app/claims/extract.py`): signatures, defaults, CLI flags, file paths, relative links, code examples, version requirements and prose sentences, each anchored to file, heading and line.
- **Scope = what the change touched.** `detect_drift` only judges claims related to a changed symbol, flag, removed/renamed path or changed manifest, so a small PR never re-checks the whole docs.
- **Verdicts**: `contradicted`, `stale_example` (does not parse / uses removed or unknown arguments), `incomplete` (new parameters undocumented), `still_true`, `unknowable` (semantic statements, left for the Phase 4 LLM). Deterministic findings carry confidence 0.95.
- **Stored in the evidence graph** as `claim` nodes (verdict, reason, evidence in `data`) with `contradicts` / `documents` edges to symbol or file nodes. A run ends at `ANALYZED`, or `SKIPPED` when no docs are affected.
- **Docs read**: root-level `*.md` and `docs/**/*.md` (max 20 files). `.doctruth.yml` globs/ignores are not parsed yet. External links are not resolved (needs network; ROADMAP). `openapi.*` and ADR-specific handling are not done yet.

## Phase 4: LLM reasoning, rewrite, critic

- **Deterministic first.** Edits for defaults, signatures, renamed symbols, CLI flag defaults, renamed paths/links and version requirements are built by rules (`pipeline/rewrite.py`) and re-verified by running the Phase 3 checks on the rewritten text. The LLM is used only for what rules cannot decide: semantic statements (judge), and rewrites of code examples and prose.
- **LLM calls are bounded and safe.** `llm/client.py`: OpenAI-compatible (Groq default, `openai/gpt-oss-120b` with `openai/gpt-oss-20b` fallback), JSON mode, 3 attempts with backoff on 429/5xx, fallback model on a rejected model, schema validation with one repair, and a call budget per run (`LLM_MAX_CALLS`, default 12). Every outgoing message passes through `llm/scrub.py` (keys, tokens, private keys, credentialed URLs).
- **Untrusted-input policy.** Repo text is only sent inside `<untrusted>` blocks; the system prompts say never to obey it. Closing tags inside the data are neutralised. Prompts are versioned files in `app/llm/prompts/` and the version ids are stored with each LLM edit.
- **Fusion rules in code** (`pipeline/reason.py: reconcile`): code wins over PR text (flagged as `sources_conflict` with a note), unresolved threads are shown to the model as "OPEN QUESTION, NOT A FACT", and a revert touching the symbol caps confidence at 0.6.
- **Critic.** An LLM edit must: cite real evidence labels, stay close to the original (similarity >= 0.5), introduce no unknown symbol/path (checked against the change set and the repo tree), and code examples must still parse and match current signatures. Only then does a separate critic prompt (temperature 0) review it. A rejected edit is retried once with the critic's feedback, then becomes a question.
- **Ask, don't guess.** Anything unresolved, below `CONFIDENCE_THRESHOLD` (0.8), removed upstream, or produced while the LLM is down/unconfigured becomes a `question` for the PR author. No `GROQ_API_KEY`/`LLM_API_KEY` means the run still works (deterministic edits + questions).
- **Storage.** The ProposedEdit lives in the claim node's `data.edit` (kind, before, after, evidence node ids, confidence, method, prompt versions). Run ends at `VERIFIED` with edit/question/LLM-call/token counts in the transition note.
- **Not done yet (ROADMAP):** `new_section` proposals for undocumented new public symbols, response caching keyed by blob SHA, per-installation token budgets, cost in currency.

## Phase 5: publishing

- Merged code (sync run): edits are applied to the docs on a bot branch `doctruth/sync-<pr>` created from the analysed commit, then one doc-sync PR is opened. Re-runs reset the branch to that commit and update the same PR, so publishing is idempotent.
- Open PR (preview run): one comment, found by a hidden marker and edited in place on every new push. Nothing is written to the repo.
- Edits are applied inside their own lines only. If the doc text no longer contains the `before` text (someone edited it since analysis) the edit is not applied; it is reported as a question. CRLF files stay CRLF.
- Questions never become edits. With no edit and no PR to comment on, the run ends NEEDS_HUMAN.
- A 401/403/404 from a write fails the job with the exact permission fix (Contents and Pull requests: Read and write); the job queue retries it.
- The bot's own branches (`doctruth/*`) are ignored by the webhook handler, so merging the doc PR cannot loop.
- Not done yet: auto-closing a doc PR whose edits became obsolete, a `.doctruth.yml` opt-out, commit-status checks.

## Phase 6: dashboard

- Server-rendered HTML from the same FastAPI app (no frontend build, no second deployment): `/dashboard` (runs + counts), `/dashboard/runs/<id>` (state timeline, evidence-graph counts, every finding with its edit/question and evidence), `/api/runs` (JSON).
- Closed by default: with no `DASHBOARD_TOKEN` the routes return 503. Login sets an HttpOnly, SameSite=Lax cookie holding an HMAC of the token (the token itself is never stored in the cookie); the JSON API also accepts `Authorization: Bearer <token>`.
- All repo-derived text is HTML-escaped and a strict CSP (`default-src 'none'`) is sent, so a hostile PR title or doc line cannot run script in the dashboard.
- Read-only: no buttons that change GitHub. Not done yet: per-installation views, filters, accept/reject of proposed edits from the UI.

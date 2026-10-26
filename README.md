# DocTruth AI

A GitHub App that keeps documentation true. It reads every pull request, finds the documentation claims the code change made false, and fixes them with evidence. When it is not sure, it asks instead of guessing.

**Live:** https://doctruth-ai.vercel.app (dashboard at `/dashboard`)

## What it does

- **Open pull request:** posts one comment listing the doc edits it would make. It updates that comment on every new push. Nothing is written to the repository.
- **Merged change:** opens a doc-sync pull request on a `doctruth/sync-<n>` branch with the fixes.
- **Unsure:** a low-confidence claim becomes a question for the author, not an edit.

## What it checks

Python, JavaScript and TypeScript code changes are compared symbol by symbol and matched against claims in Markdown files in the repository root and in `docs/`:

- function and method signatures and default values
- CLI flags and their defaults
- file paths and links (renamed or removed files)
- Python and Node version requirements
- code examples that use removed parameters
- prose statements about a changed symbol (judged by an LLM, only when rules cannot decide)

Every edit cites its evidence, stays close to the original text, and invents no symbol or path. LLM edits pass a separate critic before they are accepted.

## Stack

FastAPI, SQLAlchemy and Alembic on Postgres (Supabase), a Postgres job queue (no Redis), tree-sitter for code, Groq for the LLM, deployed on Vercel. Everything fits the free tiers.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): how a pull request flows through the system
- [Deploying from scratch](docs/DEPLOY.md): Supabase, GitHub App, Vercel, environment variables
- [Design decisions](docs/DECISIONS.md): why each phase was built the way it was
- [Roadmap](docs/ROADMAP.md): what is next and the known limits
- [Final checklist](docs/CHECKLIST.md)

## Run the tests

From the `backend` folder, install `requirements-dev.txt`, then run `pytest`. The offline benchmark is `python -m app.eval.run`.

## Safety

Webhooks are signature verified and deduplicated. Secrets are scrubbed before text goes to the LLM and before errors are stored. Repository text reaches the LLM only as untrusted data. The dashboard is read only and password protected. Setting `DRY_RUN=true` analyses everything and writes nothing to GitHub.

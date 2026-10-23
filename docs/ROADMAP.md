# Roadmap

Built and live: webhook intake, job queue, state machine, evidence graph, claim extraction, drift verdicts, deterministic and LLM edits with critic, preview comments, doc-sync pull requests, dashboard, offline benchmark, hardening.

## Next
- Per-repository settings file to opt out of paths, set the confidence threshold or switch the bot to comment-only.
- Daily LLM budget per installation, with the spend shown on the dashboard.
- Proposals for new sections when a new public symbol has no documentation.
- Close a doc-sync pull request automatically when later changes make its edits obsolete.
- Response cache keyed by file hash to cut repeat LLM calls.
- Link checking for external URLs.
- Accept and reject buttons for proposed edits in the dashboard.
- More languages for the symbol extractor.
- A larger benchmark built from real pull requests, including a scored LLM path.

## Known limits
- Only Markdown in the repository root and in `docs/` is analysed, at most 20 files per run.
- The symbol extractor covers Python, JavaScript and TypeScript.
- The dashboard has a single shared password and is read only.
- The benchmark is small and synthetic: it guards against regressions but does not prove accuracy on arbitrary repositories.

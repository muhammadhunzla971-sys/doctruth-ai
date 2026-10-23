# Final checklist

Run through this once before showing the project.

- [ ] `/health` returns status ok on the production URL.
- [ ] Dashboard asks for the password and lists runs; a wrong password is refused.
- [ ] Supabase password and GitHub private key were rotated, and the old key is deleted.
- [ ] No secret appears in the repository (check `.env` is not committed).
- [ ] The GitHub App has Contents and Pull requests read and write, and the Pull request and Push events.
- [ ] Preview: opening a pull request in the demo repository produces exactly one DocTruth comment, and a new push updates it.
- [ ] Sync: merging produces one doc-sync pull request with correct diffs; merging that pull request does not open another.
- [ ] A claim DocTruth is unsure about shows up as a question, not an edit.
- [ ] With DRY_RUN set to true a run reaches VERIFIED and nothing is written to the repository.
- [ ] `python -m app.eval.run` prints no FAIL lines and the test suite passes.
- [ ] `docs/DECISIONS.md`, `ARCHITECTURE.md`, `DEPLOY.md` and `ROADMAP.md` are current.

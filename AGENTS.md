# Working in this repo

Instructions for coding agents (Claude Code, Codex). Keep changes small and the output short.

## Code
- Write the least code that does the job. Reuse what exists (helpers, clients, components) before adding new ones.
- No speculative abstractions, config options, fallbacks or "just in case" branches.
- Match the surrounding style. Comments only where the why is not obvious; no docstrings restating the name.
- Edit in place. Don't rewrite whole files or reformat code you didn't change.

## Tests
- Don't add tests by default. Add one only for new behaviour that can break silently (data integrity,
  idempotency, money, crash recovery) or when asked.
- One focused test per behaviour. Extend an existing test file before creating a new one.
- No tests for trivial getters, wiring, UI layout or copy.
- Run only the affected suite while iterating; run the full suites once at the end.

## Tokens
- Read only the files and line ranges you need; search before reading.
- Don't re-read files you just wrote. Don't paste large outputs; use `tail`, `grep` or `-q`.
- Keep reports brief: what changed, what was run and its result, anything left to do.

## Commits and pull requests
- Commit subject: `<area>: <what changed>`, imperative, under ~72 chars. Areas: `uptime`, `dashboard`, `agents`,
  `ci`, `docs`, `repo`. Example: `uptime: sync Dependabot alerts into incidents`.
- One logical change per commit; the body (optional) says why, not what.
- PR title is plain words (`Dependabot alerts become dashboard incidents`); the body says what changed and how it was verified.
- Squash-merge into `main` with the PR number first: `#<number>: <PR title>`, e.g.
  `gh pr merge 70 --squash --subject "#70: Dependabot alerts become dashboard incidents"`.

## Layout
- `admin-dashboard/` OpsRelay uptime API (:8300) and dashboard (:3300) · `agents/` repair agent
- Architecture: `docs/system-architecture.md`. Start everything with `./dev.sh`; clear incident state with `./dev.sh reset`.
- Python suites: `admin-dashboard/backend/.venv/bin/python -m pytest -q`; agents: `agents/.venv/bin/python -m pytest agents/tests -q`.
  Dashboard types: `cd admin-dashboard/web && npx tsc --noEmit`.

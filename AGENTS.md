# Jev Linux

Jev Linux is a native Ubuntu desktop assistant. The application code is in `jev_linux/`; regression tests are in `tests/`.

## Development

- Start the application: `./run.sh`.
- Run the tests: `./scripts/test.sh`. It cleans inherited Snap editor paths before loading GTK/GI.
- Check local dependencies: `./run.sh --doctor`.
- Use the existing `.venv`, which includes Ubuntu's system GI bindings.
- API previews (`--check-api`, `--plan`) use the configured provider without executing desktop actions.
- The executed workflow diagnostic (`.venv/bin/python scripts/check_workflow.py --execute`) makes API requests, opens apps, and creates scratch notes. Use it when the change needs live verification.

## Agent skills

### Issue tracker

Track issues and specs as local Markdown under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Domain docs

Use a single-context layout: root `GLOSSARY.md` and `docs/adr/`. See `docs/agents/domain.md`.

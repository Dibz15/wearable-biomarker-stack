# Contributing

This is a hobby-scale, self-hosted project — contributions are welcome,
but keep expectations proportionate: no formal governance, no CLA, just
working code and a clear description of what changed and why.

## Before you start

- Check [`README.md`](./README.md) for the pitch and quick start, and
  [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for how the pieces
  fit together and which component you'd be touching.
- Each component has its own README with dev setup instructions:
  [`wearable-events/README.md`](./wearable-events/README.md),
  [`parser/colmi/README.md`](./parser/colmi/README.md),
  [`parser/amazfit/README.md`](./parser/amazfit/README.md).
- For anything nontrivial, opening an issue first to discuss the
  approach saves everyone time versus a large PR landing unannounced.

## Making a change

1. Fork and branch from `main`.
2. Keep the change scoped — a PR that does one thing is much easier to
   review (and to revert, if it turns out to be wrong) than one that
   bundles a feature with an unrelated refactor.
3. If you're touching `wearable-events/`, add or update tests under
   `wearable-events/tests/` for the behavior you changed. See that
   directory's existing tests for the patterns used (no live InfluxDB
   in any test — see `tests/conftest.py`).
4. Run the test suite before opening a PR:
   ```bash
   cd wearable-events
   pip install -r requirements-dev.txt
   python -m pytest tests/ -v
   ```
   CI (`.github/workflows/tests.yml`) runs this automatically on every
   PR, but catching a failure locally first is faster for you.
5. Update the relevant README if your change affects setup,
   environment variables, or the API surface.

## Code style

Match the surrounding code rather than introducing a new style
wholesale — this codebase leans toward descriptive inline comments
explaining *why* a decision was made (especially around real bugs that
were fixed, unverified assumptions, or anything derived from an
external device's undocumented behavior), not just *what* the code
does. No enforced linter/formatter currently — use your judgment and
keep diffs focused on the actual change.

## Reporting bugs / unverified data

A few things in this stack (sleep-stage codes, some Amazfit field
semantics — see [`parser/amazfit/README.md`](./parser/amazfit/README.md))
are documented as best-effort rather than fully confirmed. If you have
real device data that confirms or contradicts one of these, that's a
genuinely useful contribution even without an accompanying code change
— open an issue with what you found.

# Contributing

```bash
uv sync
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
```

- Keep the core independent of any ECM and any domain. ECM-specific code belongs in an adapter, and
  domain wording belongs in prompts and config, never in the engine.
- New adapters are welcome. Include unit tests that use a fake client (no network) and a setup guide in `docs/`.
- Never add a destructive method (delete, move, rename, overwrite) to the port or to an adapter.

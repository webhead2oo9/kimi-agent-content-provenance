# Repository guidelines

This is an independently packaged Kimi application module. Runtime imports from
Kimi must use only `kimi_agent_module_api`. Keep the main bot checkout unchanged.
Use Python 3.14+, typed async code, Ruff formatting at 100 columns, and explicit
pytest asyncio markers. Preserve original media bytes and the distinction between
missing evidence and proof of human authorship. Never commit credentials or media.

Run `uv sync --locked --extra dev`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy .`, `uv run python -m pytest -q`,
`uv build --no-sources`, and `uv run twine check dist/*` before publishing changes.
After dependency changes run `uv lock` and
`uv --preview-features audit-command audit --locked`.

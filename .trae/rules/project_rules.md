# Project Rules

## Package Manager

This project uses **uv** for Python package management.

## Lint & Format

```bash
uv run ruff check        # Check only
uv run ruff check --fix  # Check and auto-fix
```

## Test

```bash
uv run pytest tests/ -v
```

## Run Scripts

```bash
uv run python scripts/discover_bilibili.py
```
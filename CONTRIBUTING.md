# Contributing

Thanks for your interest in improving this project!

## Development setup

```bash
pip install -e ".[dev]"
```

## Before opening a pull request

- `ruff check .` – lint
- `mypy .` – type check
- `pytest` – run the test suite (dummy env is provided by `tests/conftest.py`)

## Guidelines

- Code and comments are written in English; user-facing Telegram messages are in German.
- Everything is `async`; use type hints and Pydantic models for data structures.
- Never commit secrets — configuration lives in `.env` and `kleinanzeigen-config/config.yaml`,
  both gitignored. Use the provided `*.example` files as templates.

## License

By contributing, you agree that your contributions are licensed under the project's
[AGPL-3.0-or-later](LICENSE) license.

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run pytest -q
    uv run python scripts/private_scan.py

models out="":
    uv run python scripts/check_models.py {{out}}

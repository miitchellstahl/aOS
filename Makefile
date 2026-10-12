.PHONY: sync check lint typecheck test format format-check app-sync app-check app-build

sync:
	uv sync --frozen

lint:
	uv run --frozen ruff check .

typecheck:
	uv run --frozen mypy

test:
	uv run --frozen pytest -q

format:
	uv run --frozen ruff format .
	cd app && dart format lib test

format-check:
	uv run --frozen ruff format --check .

check: lint typecheck format-check test

app-sync:
	python3 app/tool/sync_contracts.py

app-check:
	python3 app/tool/sync_contracts.py --check
	cd app && flutter pub get --enforce-lockfile
	cd app && dart format --output=none --set-exit-if-changed lib test
	cd app && flutter analyze
	cd app && flutter test --reporter expanded

app-build: app-check
	cd app && flutter build linux --release

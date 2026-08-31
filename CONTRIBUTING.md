# Contributing

Solo-maintained research project; issues and PRs welcome.

Setup: `make venv` (uv-backed). Test: `make test` (hermetic; no live API calls). Lint: `ruff check src tests && ruff format --check src tests`.

PRs target `master`, one focused change per PR, tests required for behavior changes. Never commit anything under `results/` or other run artifacts.

Contact: b@thegoatnote.com

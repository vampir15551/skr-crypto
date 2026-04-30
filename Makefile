.PHONY: help install dev test test-cov lint fmt build docs docs-serve docs-deploy clean

help:
	@echo "Common targets:"
	@echo "  make install      pip install -e ."
	@echo "  make dev          pip install -e .[dev,docs]"
	@echo "  make test         pytest"
	@echo "  make test-cov     pytest with coverage report"
	@echo "  make lint         ruff check"
	@echo "  make fmt          ruff format"
	@echo "  make build        Build wheel + sdist"
	@echo "  make docs         Build the MkDocs site (./site/) in strict mode"
	@echo "  make docs-serve   Serve docs with autoreload at http://127.0.0.1:8000"
	@echo "  make docs-deploy  One-shot deploy to GitHub Pages (gh-pages branch)"
	@echo "  make clean        Remove build/test caches"

install:
	pip install -e .

dev:
	pip install -e ".[dev,docs]"

test:
	pytest -v

test-cov:
	pytest -v --cov=skr_crypto --cov-report=term-missing --cov-report=html

lint:
	ruff check skr_crypto tests

fmt:
	ruff format skr_crypto tests

build:
	python -m build

docs:
	mkdocs build --strict

docs-serve:
	mkdocs serve

# One-shot deploy to GitHub Pages. Pushes ./site/ to the `gh-pages`
# branch and force-updates it. Use this when you can't / won't enable
# the workflow trigger in `.github/workflows/docs.yml` (e.g. private repo
# without GitHub Pro). Requires `git push` access to the repo.
docs-deploy:
	mkdocs gh-deploy --force --strict --message 'docs: deploy {sha} from $$(git rev-parse --abbrev-ref HEAD)'

clean:
	rm -rf build dist *.egg-info site
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov

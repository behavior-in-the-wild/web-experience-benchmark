.PHONY: install install-dev format lint test clean run help

# Default target
.DEFAULT_GOAL := help

# Python
PYTHON := python3
PIP := pip

# Directories
SRC_DIR := src/cwv_optimizer
TEST_DIR := tests

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install package
	$(PIP) install -e .

install-dev:  ## Install package with dev dependencies
	$(PIP) install -e ".[dev]"
	pre-commit install

format:  ## Format code with black and ruff
	black $(SRC_DIR) $(TEST_DIR)
	ruff check --fix $(SRC_DIR) $(TEST_DIR)

lint:  ## Run linters
	ruff check $(SRC_DIR) $(TEST_DIR)
	black --check $(SRC_DIR) $(TEST_DIR)
	mypy $(SRC_DIR)

test:  ## Run tests
	pytest $(TEST_DIR) -v

test-cov:  ## Run tests with coverage
	pytest $(TEST_DIR) --cov=$(SRC_DIR) --cov-report=html --cov-report=term

clean:  ## Clean build artifacts
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

run-full:  ## Run full pipeline (requires GITHUB_URL)
	cwv-optimizer full --github-url $(GITHUB_URL) --verbose

run-optimize:  ## Run optimization pipeline (requires URL, SUGGESTIONS, WORKSPACE)
	cwv-optimizer optimize --url $(URL) --parsed-suggestions $(SUGGESTIONS) --workspace-dir $(WORKSPACE) --verbose

build:  ## Build package
	$(PYTHON) -m build

publish:  ## Publish to PyPI (requires credentials)
	$(PYTHON) -m twine upload dist/*

docs:  ## Generate documentation
	@echo "Documentation generation not yet configured"

# Development shortcuts
dev: install-dev  ## Alias for install-dev

check: lint test  ## Run all checks (lint + test)

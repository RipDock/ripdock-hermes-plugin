SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

PYTHON ?= python3

.PHONY: help test lint clean

help:
	@printf '%s\n' \
		'Hermes plugin commands:' \
		'  make help   Show this help' \
		'  make test   Run Hermes plugin tests' \
		'  make lint   Syntax-check Python sources' \
		'  make clean  Remove Python caches and build output'

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_*.py'

lint:
	$(PYTHON) -c "import ast, pathlib; roots = [pathlib.Path(p) for p in ('__init__.py', 'backend', 'runtime', 'shared', 'tests')]; files = [r for r in roots if r.is_file()] + [p for r in roots if r.is_dir() for p in r.rglob('*.py')]; [ast.parse(p.read_text(), filename=str(p)) for p in files]"

clean:
	find . \( -name '__pycache__' -o -name '*.pyc' \) -prune -exec rm -rf {} +
	rm -rf dist build

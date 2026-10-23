.PHONY: install run test demo lint clean

install:
	pip install -r requirements.txt

run:
	python agent_backend.py

test:
	python -m pytest tests/ -v --tb=short --cov=src --cov-report=term-missing --cov-report=html

demo:
	python scripts/demo.py

lint:
	python -m py_compile src/config.py
	python -m py_compile src/server.py
	@echo "Lint OK"

clean:
	rm -rf __pycache__ src/__pycache__ tests/__pycache__ .pytest_cache htmlcov .coverage
	find . -name '*.pyc' -delete

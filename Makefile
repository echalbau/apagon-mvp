PY ?= .venv/bin/python

.PHONY: install demo test status

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

demo:
	$(PY) -m apagon demo

test:
	$(PY) -m pytest -q

status:
	$(PY) -m apagon status

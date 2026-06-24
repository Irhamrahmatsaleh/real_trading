PYTHON ?= python3
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn

.PHONY: install serve test clean

$(PY):
	$(PYTHON) -m venv $(VENV)

$(VENV)/.installed: requirements.txt $(PY)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	touch $(VENV)/.installed

install: $(VENV)/.installed

serve: install
	$(UVICORN) app.main:app --host 0.0.0.0 --port $${PORT:-8000}

test: install
	$(PY) -m pytest

clean:
	rm -rf $(VENV) .pytest_cache

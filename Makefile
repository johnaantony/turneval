# Every target routes through PYTHONPATH=src rather than the editable install.
#
# Why: on macOS the .pth file uv writes into the venv gets the UF_HIDDEN file
# flag, and CPython 3.12's site.py silently skips hidden .pth files. The result
# is an editable install that reports success and then cannot be imported, with
# no error message anywhere. The flag comes back every time uv reinstalls the
# project, so `unhide` is a fix you would have to keep re-running. PYTHONPATH
# sidesteps the whole thing.

PY := .venv/bin/python
export PYTHONPATH := src

.PHONY: help sync unhide test scenarios score console dev clean

help:
	@echo "make sync       install dependencies"
	@echo "make test       run the test suite"
	@echo "make scenarios  list the scenario set"
	@echo "make console    talk to the agent from the terminal (needs .env)"
	@echo "make dev        run the agent as a LiveKit worker (needs .env)"
	@echo "make score      score every trace in data/traces"
	@echo "make unhide     clear the macOS hidden flag on the venv .pth files"

sync:
	uv sync
	@$(MAKE) --no-print-directory unhide

unhide:
	@chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true

test:
	$(PY) -m unittest discover -s tests -v

scenarios:
	$(PY) -m turneval.cli scenarios

# Pass a scenario through: make console SCENARIO=ambiguous-date
console:
	TURNEVAL_SCENARIO_ID=$(SCENARIO) $(PY) agents/booking_agent.py console

dev:
	TURNEVAL_SCENARIO_ID=$(SCENARIO) $(PY) agents/booking_agent.py dev

score:
	$(PY) -m turneval.cli score --label $(or $(LABEL),local)

clean:
	rm -rf data/traces/* data/bookings/*

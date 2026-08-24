# ══════════════════════════════════════════════════════════════════════════════
# ATLAS PROXY FABRIC v4
#
# `make doctor` is the gate command. It runs gate_check.py BEFORE pytest, because
# a green test suite is not evidence on its own: during the sync loss `pytest -q`
# reported "10 passed" while atlas/core/ did not exist -- the isolation tests were
# globbing an empty directory and passing VACUOUSLY (ADR-010).
# ══════════════════════════════════════════════════════════════════════════════
PY      ?= python3
PYTEST  ?= $(PY) -m pytest
TOOLS   := engineering/tools

.DEFAULT_GOAL := help
.PHONY: help doctor gate test test-unit test-integration lint typecheck \
        install verify-evidence state clean legacy-baseline sources-audit

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-20s\033[0m %s\n", $$1, $$2}'

# ── the gate ──────────────────────────────────────────────────────────────────
doctor: gate test  ## FULL GATE: evidence integrity, then the test suite
	@echo ""
	@echo "═══════════════════════════════════════════════════════════════════"
	@echo " doctor: evidence verified AND tests green."
	@echo " Neither alone is sufficient (ADR-010)."
	@echo "═══════════════════════════════════════════════════════════════════"

gate:  ## verify declared evidence exists + tests are not vacuous (ADR-010)
	@$(PY) $(TOOLS)/gate_check.py

test:  ## run the whole suite
	@$(PYTEST) -q

test-unit:  ## unit tests only (must need no network)
	@$(PYTEST) -q atlas/tests/unit

test-integration:  ## integration tests (may touch sqlite/network)
	@$(PYTEST) -q atlas/tests/integration

# ── evidence tooling ──────────────────────────────────────────────────────────
verify-evidence:  ## re-derive the Phase-0 figures and reconcile them
	@$(PY) $(TOOLS)/verify_bug_lines.py
	@$(PY) $(TOOLS)/verify_geonode_parser.py

sources-audit:  ## re-probe the legacy source list (NETWORK; new dated snapshot)
	@$(PY) $(TOOLS)/probe_legacy_sources.py
	@$(PY) $(TOOLS)/reprobe_empty.py

legacy-baseline:  ## re-measure the legacy baseline (NETWORK, slow)
	@$(PY) $(TOOLS)/measure_baseline.py

state:  ## print the resume state at a glance
	@$(PY) -c "import json;s=json.load(open('engineering/TASK_STATE.json'));\
print('phase      :',s['current_phase']);\
print('gates      :',{k:v for k,v in s['phase_gate_status'].items() if v!='TODO'});\
print('tasks done :',sum(1 for t in s['tasks'] if t['status']=='DONE'),'/',len(s['tasks']));\
print('next       :',s['next_action'][:120])"

# ── housekeeping ──────────────────────────────────────────────────────────────
install:  ## install runtime + test dependencies
	@$(PY) -m pip install -r requirements.txt

lint:  ## ruff if available (advisory only; the fitness tests are authoritative)
	@$(PY) -m ruff check atlas/ 2>/dev/null || echo "  (ruff not installed -- skipped)"

typecheck:  ## mypy if available
	@$(PY) -m mypy atlas/ 2>/dev/null || echo "  (mypy not installed -- skipped)"

clean:  ## remove caches (never touches engineering/ -- that is the evidence base)
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .mypy_cache .ruff_cache
	@echo "caches removed; engineering/ untouched"

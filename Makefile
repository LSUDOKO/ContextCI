.PHONY: help demo test gate datahub-up datahub-down token clean
.DEFAULT_GOAL := help

PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
GMS_PORT ?= 8081

help:  ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

demo:  ## Boot DataHub, load lineage, run the gate (start here)
	@./scripts/demo.sh

test:  ## Run the test suite (no network, no DataHub, no LLM)
	@$(PY) -m pytest tests/ -q

gate:  ## Run the gate against a diff: make gate DIFF=examples/breaking_change.diff
	@$(PY) -m src.main --diff $(or $(DIFF),examples/demo_sample_hive.diff)

datahub-up:  ## Start DataHub only
	@DATAHUB_MAPPED_GMS_PORT=$(GMS_PORT) $(PY) -m datahub docker quickstart

datahub-down:  ## Stop DataHub containers
	@docker ps -q --filter name=datahub | xargs -r docker stop

token:  ## Mint a DataHub access token into .env (needs auth enabled)
	@$(PY) scripts/make_datahub_token.py

clean:  ## Remove local run artifacts
	@rm -f contextci-report.json
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +

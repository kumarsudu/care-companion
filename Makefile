# Makefile for CareCompanion ADK Agent

.PHONY: install playground run test

install:
	uv sync

playground:
	uv run adk web app --host 127.0.0.1 --port 18081 --reload_agents

run:
	uv run adk api_server app --host 127.0.0.1 --port 8090 --reload_agents

test:
	uv run pytest tests/unit tests/integration

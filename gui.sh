#!/usr/bin/env bash
# gui.sh — запуск ProxyPilot (GUI-тестер прокси)
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv && .venv/bin/pip install -q free-proxy requests
fi
exec .venv/bin/python proxy_gui.py "$@"

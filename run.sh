#!/usr/bin/env sh
# One command: install deps (into .venv) and start the runtime + viewer on http://localhost:8080.
# The world server runs separately:  python world_server.py
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
PY=python3; command -v $PY >/dev/null 2>&1 || PY=python
if [ ! -d .venv ]; then $PY -m venv .venv; fi
if [ -f .venv/bin/python ]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python; fi
$VPY -m pip install -q -r requirements.txt
exec $VPY -m flightrec serve

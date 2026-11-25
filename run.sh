#!/usr/bin/env sh
# One command: install deps and start the runtime + viewer on http://localhost:8080.
# Works without an LLM key: the recorded runs in runs/final are loaded and can be viewed and replayed.
# The Northwind world runs separately:  python world_server.py      (Windows: double-click run.bat instead)
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
PY=""
for c in python3 python py; do  # first one that really runs (Windows has a python3 stub that doesn't)
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys; assert sys.version_info >= (3, 11)" >/dev/null 2>&1; then PY=$c; break; fi
done
[ -n "$PY" ] || { echo "Python 3.11+ not found"; exit 1; }
venv_py() { if [ -x .venv/bin/python ]; then echo .venv/bin/python; elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; fi; }
VPY=$(venv_py)
if [ -z "$VPY" ] || ! "$VPY" -m pip --version >/dev/null 2>&1; then
  rm -rf .venv
  if "$PY" -m venv .venv >/dev/null 2>&1 && [ -n "$(venv_py)" ] && "$(venv_py)" -m pip --version >/dev/null 2>&1; then
    VPY=$(venv_py)
  else  # some Python installs can't build venvs (no ensurepip): use the system interpreter instead
    rm -rf .venv
    echo "(no virtualenv available; using $PY directly)"
    VPY=$PY
  fi
fi
"$VPY" -m pip install -q -r requirements.txt 2>/dev/null || "$VPY" -m pip install -q --user -r requirements.txt
echo "Flight Recorder: http://localhost:${PORT:-8080}"
exec "$VPY" -m flightrec serve

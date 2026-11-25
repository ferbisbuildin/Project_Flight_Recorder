@echo off
rem Windows: double-click to start the runtime + viewer on http://localhost:8080.
rem Works without an LLM key: recorded runs in runs\final are loaded and can be viewed and replayed.
rem The Northwind world runs separately:  python world_server.py
cd /d "%~dp0"
if not exist .env copy .env.example .env >nul

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

rem 1) reuse a working .venv
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip --version >nul 2>nul && goto :venv
  rmdir /s /q .venv
)
rem 2) or build one
%PY% -m venv .venv >nul 2>nul
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip --version >nul 2>nul && goto :venv
  rmdir /s /q .venv
)
rem 3) or fall back to the system Python (some installs can't build virtualenvs)
echo (no virtualenv available; using the system Python)
set "VPY=%PY%"
goto :install

:venv
set "VPY=.venv\Scripts\python.exe"

:install
%VPY% -m pip install -q -r requirements.txt || %VPY% -m pip install -q --user -r requirements.txt || goto :fail
start "" http://localhost:8080
%VPY% -m flightrec serve
goto :eof

:fail
echo Could not install requirements. Is Python 3.11+ installed and on PATH?
pause

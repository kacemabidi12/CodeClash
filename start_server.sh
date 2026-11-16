#!/usr/bin/env bash
# CodeClash game server. Run it on the MacBook, from Terminal.app:
#
#     bash start_server.sh
#
# Quick 4-round demo:   CODECLASH_TOTAL_ROUNDS=4 bash start_server.sh
# The server uses only the Python standard library: nothing to install.
set -e
cd "$(dirname "$0")"

PY=python3
if [ -x venv/bin/python ]; then PY=venv/bin/python; fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "python3 was not found. Install Python 3.11+ from https://www.python.org/downloads/"
  exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "CodeClash needs Python 3.10 or newer (3.11+ recommended). Found: $("$PY" --version 2>&1)"
  echo "Install a newer Python from https://www.python.org/downloads/ and run this script again."
  exit 1
}

if [ "$(uname)" = "Darwin" ] && [ "$TERM_PROGRAM" = "vscode" ]; then
  echo "Note: you are in VS Code's terminal. If UDP fails with 'No route to host', allow VS Code in"
  echo "System Settings > Privacy & Security > Local Network, or run this script from Terminal.app."
fi

exec "$PY" -m server.main_server "$@"

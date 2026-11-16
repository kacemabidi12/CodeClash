#!/usr/bin/env bash
# CodeClash player app — Git Bash on Windows (or a Mac/Linux terminal):
#
#     bash start_client.sh            # http://localhost:8501
#     bash start_client.sh 8502       # another port (several clients on one computer, development only)
#
# First run: creates ./venv and installs Streamlit (needs internet once).
set -e
cd "$(dirname "$0")"

venv_python() {
  if [ -x venv/Scripts/python.exe ]; then echo venv/Scripts/python.exe; else echo venv/bin/python; fi
}

if [ ! -x venv/Scripts/python.exe ] && [ ! -x venv/bin/python ]; then
  if command -v py >/dev/null 2>&1; then BASE="py -3"
  elif command -v python3 >/dev/null 2>&1; then BASE=python3
  else BASE=python; fi
  echo "Creating the virtual environment (first run only)..."
  $BASE -m venv venv
fi
PY="$(venv_python)"

"$PY" -m pip install --disable-pip-version-check -q -r requirements.txt
URL="http://localhost:${1:-8501}"
echo "Starting CodeClash on $URL — type the MacBook's IP on the first screen. Ctrl+C to quit."
(  # open the browser once the app had a few seconds to start
  sleep 3
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v cmd.exe >/dev/null 2>&1; then cmd.exe //c start "" "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) >/dev/null 2>&1 &
exec "$PY" -m streamlit run app.py --server.address 127.0.0.1 --server.port "${1:-8501}"

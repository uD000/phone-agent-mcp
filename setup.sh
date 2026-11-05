#!/bin/sh
# One-step setup on a Mac: Python environment, the screen-text helper, the optional Contacts helper, then a health check.
set -eu
cd "$(dirname "$0")"
[ "$(uname -s)" = Darwin ] || { echo "phone-agent needs macOS"; exit 1; }
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null || { echo "Python 3.11+ is required (brew install python)"; exit 1; }
echo "==> Python environment"
"$PY" -m venv venv
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -e .
echo "==> Helpers"
mkdir -p bin
if command -v swiftc >/dev/null 2>&1; then
  swiftc -O tools/ocr.swift -o bin/ocr && echo "built bin/ocr"
  sh tools/build_contacts_app.sh || echo "(Contacts helper skipped)"
else
  echo "swiftc not found: install Xcode, then re-run ./setup.sh (screen-text OCR needs it)"
fi
chmod +x bin/pa-kill bin/pa-resume
echo "==> Health check"
venv/bin/phone-agent doctor || true
cat <<MSG

Register it with Claude Code:
  claude mcp add phone-agent -- "$PWD/venv/bin/python" -m phoneagent.mcp_server
Then start a NEW Claude Code session, and ask it to "list my phones".
Live view of the phone:  venv/bin/phone-agent console
Emergency stop:          bin/pa-kill   (resume: bin/pa-resume)
MSG

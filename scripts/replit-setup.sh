#!/usr/bin/env sh
# Install the app's Python packages into the project's own environment (.venv).
#
# Replit's Python lives in the read-only Nix store and its pip is set up for
# "--user" installs, so a plain `pip install` fails there. A project .venv
# avoids both. Safe to run again: it only installs what's missing.
#
#   sh scripts/replit-setup.sh
#   . .venv/bin/activate            # then `flask`, `pytest` etc. use .venv
set -e
cd "$(dirname "$0")/.."
export PIP_USER=false PIP_DISABLE_PIP_VERSION_CHECK=1
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Python packages ready in .venv"

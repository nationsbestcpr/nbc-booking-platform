#!/usr/bin/env sh
# Start the published app on Replit: check settings, make sure the packages
# are there, bring the database up to date, then start the web server.
# Every step prints a "[start]" line, so the deployment's Logs tab shows
# exactly where a failed start stopped.
set -e
cd "$(dirname "$0")/.."

echo "[start] APP_ENV=${APP_ENV:-(not set: the app will treat itself as local)}"
[ -n "${SECRET_KEY:-}" ]   && echo "[start] SECRET_KEY is set"   || echo "[start] SECRET_KEY is NOT set"
[ -n "${DATABASE_URL:-}" ] && echo "[start] DATABASE_URL is set" || echo "[start] DATABASE_URL is NOT set"
if [ -z "${DATABASE_URL:-}" ]; then
  echo "[start] ERROR: the published app has no database. Create the production database in Replit's Database tool, then republish." >&2
  exit 1
fi

# The build creates .venv, but the published copy may not include it.
if [ ! -x .venv/bin/gunicorn ]; then
  echo "[start] .venv is missing here; installing the packages now"
  sh scripts/replit-setup.sh
fi

echo "[start] bringing the database up to date"
.venv/bin/flask --app wsgi db upgrade

# Listen on 5000 (the [[ports]] entry in .replit) and also on PORT if Replit
# sets a different one: the readiness check fails with 503 when the app isn't
# listening where Replit's proxy looks.
BINDS="--bind 0.0.0.0:5000"
if [ -n "${PORT:-}" ] && [ "$PORT" != "5000" ]; then
  BINDS="$BINDS --bind 0.0.0.0:$PORT"
fi
echo "[start] PORT from Replit: ${PORT:-(not set)}; listening with $BINDS"
exec .venv/bin/gunicorn $BINDS --workers 2 --access-logfile - --error-logfile - wsgi:app

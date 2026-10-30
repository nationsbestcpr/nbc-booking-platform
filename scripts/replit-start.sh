#!/usr/bin/env sh
# Start the published app on Replit: check settings, make sure the packages
# are there, bring the database up to date, then start the web server.
#
# Every step prints a "[start]" line (the deployment's Logs tab shows them).
# If a step fails outside production, the published address shows the whole
# start-up log as a page (scripts/startup_report.py) instead of Replit's
# "did not become ready in time". In production a failed start just stops,
# so Replit keeps the previous version running.
set -u
cd "$(dirname "$0")/.."

LOG=.start.log
: > "$LOG"
say() { echo "[start] $*" | tee -a "$LOG"; }

# Listen on 5000 (the [[ports]] entry in .replit) and on PORT if Replit sets
# a different one: the readiness check gets 503 when nothing listens where
# Replit's proxy looks.
PORTS="5000"
if [ -n "${PORT:-}" ] && [ "$PORT" != "5000" ]; then PORTS="5000 $PORT"; fi

fail() {
  say "FAILED: $*"
  if [ "${APP_ENV:-local}" = "production" ]; then exit 1; fi
  say "showing this log at the published address (not production)"
  exec python3 scripts/startup_report.py "$LOG" $PORTS
}

# Run a step with a time limit, copying its output into the log.
# Usage: step SECONDS command...
step() {
  limit=$1; shift
  if command -v timeout >/dev/null 2>&1; then set -- timeout "$limit" "$@"; fi
  { "$@" 2>&1; echo $? > .start.rc; } | tee -a "$LOG"
  rc=$(cat .start.rc); rm -f .start.rc
  [ "$rc" = 124 ] && say "(stopped after ${limit}s: it was stuck)"
  return "$rc"
}

say "PORT from Replit: ${PORT:-(not set)}; will listen on: $PORTS"
say "APP_ENV=${APP_ENV:-(not set: the app will treat itself as local)}"
[ -n "${SECRET_KEY:-}" ]   && say "SECRET_KEY is set"   || say "SECRET_KEY is NOT set"
[ -n "${DATABASE_URL:-}" ] && say "DATABASE_URL is set" || say "DATABASE_URL is NOT set"
[ -n "${DATABASE_URL:-}" ] || fail "the published app has no database. Create the production database in Replit's Database tool, then republish."

# The build creates .venv, but the published copy may not include it.
if [ ! -x .venv/bin/gunicorn ]; then
  say ".venv is missing here; installing the packages now"
  step 240 sh scripts/replit-setup.sh || fail "installing the packages"
fi

say "checking the app loads with these settings"
step 60 .venv/bin/python -c "import wsgi" || fail "the app could not load (see the error above)"

say "bringing the database up to date"
step 120 .venv/bin/flask --app wsgi db upgrade || fail "the database update (see the error above)"

BINDS=""
for p in $PORTS; do BINDS="$BINDS --bind 0.0.0.0:$p"; done
say "starting the web server"
exec .venv/bin/gunicorn $BINDS --workers 2 --access-logfile - --error-logfile - wsgi:app

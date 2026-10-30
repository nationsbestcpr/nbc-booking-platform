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
# A published app must never run as local development (built-in secret key,
# local-only commands allowed).
case "${APP_ENV:-}" in
  staging|production) ;;
  *) fail "APP_ENV must be staging or production in the published app's secrets (Publishing → Adjust settings → Secrets), then republish." ;;
esac
key="${SECRET_KEY:-}"
[ "${#key}" -ge 32 ] || fail "SECRET_KEY (32+ random characters) is missing from the published app's secrets (Publishing → Adjust settings → Secrets), then republish."

# Everything runs as `.venv/bin/python -m ...`: in the published copy the
# flask/gunicorn launcher scripts can point at Replit's own Python, which
# can't see the packages ("No module named 'flask'").
PY=.venv/bin/python

# The build creates .venv, but the published copy may not include it.
if ! $PY -c "import flask, gunicorn" >/dev/null 2>&1; then
  say ".venv is missing or incomplete here; installing the packages now"
  step 240 sh scripts/replit-setup.sh || fail "installing the packages"
fi

say "checking the app loads with these settings"
step 60 $PY -c "import wsgi" || fail "the app could not load (see the error above)"

say "bringing the database up to date"
step 120 $PY -m flask --app wsgi db upgrade || fail "the database update (see the error above)"

# The preset admin account from the SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD
# secrets (does nothing when they aren't set).
say "checking the preset admin account"
step 30 $PY -m flask --app wsgi users ensure-superadmin || fail "the preset admin account (see the message above)"

# The background worker (Airtable sync, lapsed holds, GHL/Stripe queue) runs
# beside the web server; if it ever stops, it starts again after 10 seconds.
# Its lines start with "[worker]" in the Logs tab.
say "starting the background worker"
( while true; do $PY -m flask --app wsgi worker run; echo "[worker] stopped; restarting in 10 s"; sleep 10; done ) &

BINDS=""
for p in $PORTS; do BINDS="$BINDS --bind 0.0.0.0:$p"; done
say "starting the web server"
exec $PY -m gunicorn $BINDS --workers 2 --access-logfile - --error-logfile - wsgi:app

#!/bin/sh
# start.sh -- one command for a new user: build and start the tank stack.
#
#     sh scripts/start.sh            build, then start (or update and restart)
#     sh scripts/start.sh --yes      same, without the "already running" question
#     sh scripts/start.sh stop       stop the new-user services (data is kept)
#     sh scripts/start.sh status     list them with their health
#     sh scripts/start.sh telegram   phone alerts: asks for the bot token, writes .env, restarts, test alert
#     sh scripts/start.sh golive     make the setup page's practice calendar and settings live, restart
#     sh scripts/start.sh check      health check (cue data, controller, thermometer, alerts); the setup
#                                    page shows the result in step 3
#
# Starts exactly the services a new user needs (data/docs/QUICKSTART.md):
#   tank       tank-controller   the controller (observe-only until you switch outputs on)
#                                and the tracking page  http://127.0.0.1:8095/
#   lifeguard  tank-lifeguard    independent watchdog: alerts when the controller or the
#                                thermometers go quiet (switches nothing until you arm it)
#   autoheal   tank-autoheal     restarts either of them if it hangs
#   setup      tank-setup        onboarding page http://127.0.0.1:8098/ (staging copy)
# and none of the optional ones (the Android/Inkbird emulator, the practice
# sandbox, the night camera recorder). The daily Telegram feed runs inside the
# tank container once reeflive_token and a chat id are set in .env.
#
# On first run it copies .env.example to .env and seeds the onboarding page's
# staging folder (staging/data) from data/. Needs only Docker; no Python on the host.
# Windows: run it from Git Bash (comes with Git for Windows) in the repo folder.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
SERVICES="tank lifeguard autoheal setup"
YES=0
CMD=start
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    start|stop|status|telegram|golive|check) CMD=$a ;;
    *) echo "usage: sh scripts/start.sh [start|stop|status|telegram|golive|check] [--yes]" >&2; exit 2 ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop (or the docker service) and try again." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Made .env from .env.example. Alerts are off until you fill it in (see QUICKSTART)."
fi

dc() { docker compose --profile setup "$@"; }

# The jobs the setup page cannot do itself (scripts/host_helper.py), in a throwaway
# container of the setup image with this folder mounted, so they can write .env and data/.
helper() {
  docker image inspect anemones-setup >/dev/null 2>&1 || dc build setup
  HOSTROOT=$(pwd -W 2>/dev/null || pwd)   # Git Bash on Windows needs the C:/ form
  TTY=""; WRAP=""
  if [ -t 0 ]; then
    TTY="-it"
    # Git Bash's own terminal needs winpty for an interactive container
    [ -n "${MSYSTEM:-}" ] && command -v winpty >/dev/null 2>&1 && WRAP="winpty"
  fi
  # shellcheck disable=SC2086
  MSYS_NO_PATHCONV=1 $WRAP docker run --rm $TTY -v "$HOSTROOT:/repo" -w /repo anemones-setup     python scripts/host_helper.py "$@"
}

case "$CMD" in
  stop)
    # shellcheck disable=SC2086
    dc stop $SERVICES
    exit 0 ;;
  status)
    # shellcheck disable=SC2086
    dc ps $SERVICES
    exit 0 ;;
  telegram)
    if helper telegram; then
      echo "Restarting the controller and the watchdog so they read the new .env..."
      dc up -d tank lifeguard >/dev/null
      [ -n "$(docker ps -q --filter name=^tank-setup$)" ] && dc up -d setup >/dev/null
      echo "Done. Real alerts now go to the same chat as the test."
    fi
    exit 0 ;;
  golive)
    if helper golive; then
      echo "Restarting the controller so it follows the new calendar..."
      dc restart tank >/dev/null
      echo "Waiting for its first minute (up to 2 minutes)..."
      i=0; while [ $i -lt 24 ]; do sleep 5; i=$((i+1))
        [ -n "$(find data/tank_status.json -newermt '-30 seconds' 2>/dev/null)" ] && break; done
      helper check || true
    fi
    exit 0 ;;
  check)
    dc ps $SERVICES
    echo
    helper check || true
    if [ -n "$(docker ps -q --filter name=^tank-controller$)" ]; then
      echo; echo "Plugs (reading only, nothing is switched):"
      dc exec -T tank python /app/scripts/conductor.py health || true
    fi
    exit 0 ;;
esac

# Re-running is how you update, but it rebuilds and restarts the containers.
if [ "$YES" -ne 1 ] && [ -n "$(docker ps -q --filter name=^tank-controller$)" ]; then
  printf "The controller is already running. Rebuild and restart it now? [y/N] "
  read -r ans || ans=
  case "$ans" in y|Y|yes|YES) ;; *) echo "Nothing changed."; exit 0 ;; esac
fi

echo "Building images (the first time takes a few minutes)..."
dc build tank lifeguard setup

# First run: fetch the reef cue tables into data/ (CUE_TABLES_* in .env; does
# nothing when data/ already holds a calendar or the locale tables).
dc run --rm --no-deps tank python /app/scripts/cue_import.py ensure --data /data || \
  echo "Could not fetch the cue tables; the onboarding page will have no reefs until it works."

# The onboarding page works on a staging copy, never on data/ (SETUP_STAGING=1).
if [ ! -d staging/data ]; then
  echo "Seeding the onboarding page's staging copy (staging/data)..."
  HOSTROOT=$(pwd -W 2>/dev/null || pwd)   # Git Bash on Windows needs the C:/ form
  MSYS_NO_PATHCONV=1 docker run --rm -v "$HOSTROOT:/repo" -w /repo anemones-setup \
    python scripts/setup_web.py seed --from data --to staging/data
fi

# shellcheck disable=SC2086
dc up -d $SERVICES

cat <<'EOF'

Started. Give it two minutes, then open:
  Onboarding page  http://127.0.0.1:8098/   (works on staging/data; see QUICKSTART step 7)
  Tracking page    http://127.0.0.1:8095/   (your everyday home page)
Check health any time with:  sh scripts/start.sh status
EOF

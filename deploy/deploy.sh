#!/usr/bin/env bash
#
# Deploy the bot and render the landing page.
#
#   sudo -u cohort ./deploy/deploy.sh
#   sudo -u cohort ./deploy/deploy.sh --skip-restart    # build only
#
# Idempotent: safe to re-run. Verification runs before the service is
# restarted, so a bad config fails the deploy instead of taking the bot down.

set -euo pipefail

APP_DIR="${APP_DIR:-/srv/cohort_distribution}"
VENV="$APP_DIR/.venv"
BRANCH="${BRANCH:-main}"
SERVICE="${SERVICE:-cohort-bot}"
SKIP_RESTART=0

for arg in "$@"; do
  case "$arg" in
    --skip-restart) SKIP_RESTART=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\n==> %s\n' "$1"; }

cd "$APP_DIR"

log "Backing up the database before touching anything"
if [[ -x "$VENV/bin/python" ]]; then
  "$VENV/bin/python" scripts/backup.py || echo "    (no database yet, continuing)"
fi

log "Fetching $BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

log "Syncing the virtualenv"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

log "Checking configuration and data"
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "    .env is missing. Copy .env.example and fill it in." >&2
  exit 1
fi
chmod 600 "$APP_DIR/.env"
"$VENV/bin/python" scripts/verify.py

log "Rendering the landing page"
# The landing page is one self-contained HTML file — CSS inline, no assets to
# copy. Its only deploy-specific value is the bot username, substituted here so
# the repo never hardcodes one.
BOT_USERNAME="$(grep -E '^TELEGRAM_BOT_USERNAME=' "$APP_DIR/.env" | cut -d= -f2- | tr -d '"'"'"' ')"
if [[ -z "$BOT_USERNAME" ]]; then
  echo "    TELEGRAM_BOT_USERNAME is empty in .env" >&2
  exit 1
fi
rm -rf "$APP_DIR/landing/dist"
mkdir -p "$APP_DIR/landing/dist"
sed "s/__BOT_USERNAME__/${BOT_USERNAME}/g" \
  "$APP_DIR/landing/index.html" > "$APP_DIR/landing/dist/index.html"
echo "    landing/dist ready (point your web server's document root at it)"

if [[ "$SKIP_RESTART" -eq 1 ]]; then
  log "Skipping restart as requested"
  exit 0
fi

log "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"
sleep 2
sudo systemctl is-active --quiet "$SERVICE" \
  && echo "    $SERVICE is running" \
  || { echo "    $SERVICE failed to start; see: journalctl -u $SERVICE -n 50" >&2; exit 1; }

log "Done"

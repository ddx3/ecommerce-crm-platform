#!/bin/sh
# Installing the shop-helper service on the VPS (idempotent). As root, on the server, FROM A FULL
# COPY of the project:
#   sh ops/install.sh            [--force]
# (the script takes the auto-fix policy from ../backend/analyst/, so the ops/ folder alone is not
# enough).
# The helper code is copied to /opt/shop-helper - OUTSIDE the project's git copy, so that a release
# `git pull`
# does not conflict with these files. Updating the code = run `sh ops/install.sh` again.
#
# All input files are checked BEFORE any changes; files are installed atomically (temporary file +
# mv).
# If an auto-fix rollout is in progress (or an inflight.json marker remains), the service is NOT
# restarted
# without --force: a restart in the middle of a rollout interrupts it.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${SHOP_PROJECT:-/opt/shop}"
HELPER_DIR="${HELPER_DIR:-/opt/shop-helper}"
AUTOFIX_DIR="${SHOP_AUTOFIX_DIR:-/opt/shop-autofix}"
FORCE=0
[ "$1" = "--force" ] && FORCE=1

for f in "$SRC/shop_helper.py" "$SRC/codefix_host.py" "$SRC/safeio.py" "$SRC/shop-helper.service" \
         "$SRC/../backend/analyst/codefix_policy.py"; do
    [ -f "$f" ] || { echo "File $f is missing — run install.sh from a full copy of the project (git clone/pull), not from a separate ops/ folder." >&2; exit 1; }
done
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -c "import ast,sys; [ast.parse(open(p, encoding='utf-8').read()) for p in sys.argv[1:]]" \
    "$SRC/shop_helper.py" "$SRC/codefix_host.py" "$SRC/safeio.py" "$SRC/../backend/analyst/codefix_policy.py"

if [ "$FORCE" -ne 1 ]; then
    if [ -f "$AUTOFIX_DIR/inflight.json" ]; then
        echo "There is an unfinished deployment ($AUTOFIX_DIR/inflight.json) — not restarting. Wait for the recovery or use --force." >&2
        exit 1
    fi
    if python3 - "$PROJECT/ops-queue/results/status.json" <<'PY'
import json, sys
try:
    job = json.load(open(sys.argv[1], encoding='utf-8')).get('job')
except Exception:
    job = None
sys.exit(0 if job else 1)
PY
    then
        echo "The helper is running an auto-fix task right now — not restarting. Wait for it to finish or use --force." >&2
        exit 1
    fi
fi

mkdir -p "$PROJECT/ops-queue/requests" "$PROJECT/ops-queue/results" "$PROJECT/ops-queue/done" \
         "$PROJECT/ops-queue/artifacts" "$PROJECT/backups"
install -d -m 0755 "$HELPER_DIR"
install -d -m 0700 "$AUTOFIX_DIR"
for sub in wt applied rejected build; do install -d -m 0700 "$AUTOFIX_DIR/$sub"; done

put() {  # put <source> <destination> <mode>: first alongside, then an atomic swap
    install -m "$3" "$1" "$2.new"
    mv -f "$2.new" "$2"
}
put "$SRC/shop_helper.py" "$HELPER_DIR/shop_helper.py" 0755
put "$SRC/codefix_host.py" "$HELPER_DIR/codefix_host.py" 0755
put "$SRC/safeio.py" "$HELPER_DIR/safeio.py" 0644
# We take the auto-fix policy from the repository and put it OUTSIDE git: the agent cannot change
# it, the helper checks patches against its own copy.
put "$SRC/../backend/analyst/codefix_policy.py" "$HELPER_DIR/codefix_policy.py" 0644

# Signing key for auto-fix tasks: known only to the helper and the analyst container
# (docker-compose.yml mounts this file
# only into analyst). Without a signature the helper executes no auto-fix task.
if [ ! -s "$AUTOFIX_DIR/codefix.key" ]; then
    umask 077
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$AUTOFIX_DIR/codefix.key"
    echo "Signing key created: $AUTOFIX_DIR/codefix.key"
fi
chmod 0600 "$AUTOFIX_DIR/codefix.key"

sed "s#/opt/shop/ops/shop_helper.py#$HELPER_DIR/shop_helper.py#" "$SRC/shop-helper.service" \
    > /etc/systemd/system/shop-helper.service.new
mv -f /etc/systemd/system/shop-helper.service.new /etc/systemd/system/shop-helper.service
chmod 0644 /etc/systemd/system/shop-helper.service

systemctl daemon-reload
systemctl enable shop-helper.service
systemctl restart shop-helper.service
sleep 4
systemctl --no-pager --lines=8 status shop-helper.service || true
echo "--- status.json:"
cat "$PROJECT/ops-queue/results/status.json" 2>/dev/null || echo "(not there yet — wait a few seconds)"
echo

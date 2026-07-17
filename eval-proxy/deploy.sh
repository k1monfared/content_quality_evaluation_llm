#!/bin/sh
# deploy.sh
#
# Automates the scriptable, non-secret deploy steps for the eval-proxy Worker:
#   - verifies you are logged in to Cloudflare (it never logs in for you),
#   - installs npm dependencies if they are missing,
#   - creates the D1 database only if it does not already exist,
#   - applies schema.sql to the remote D1 (safe to re-run),
#   - prints the exact secret-setting commands for you to run yourself,
#   - runs wrangler deploy.
#
# It contains NO secret values. It never reads, stores, or passes your
# POE_API_KEY, RESEND_API_KEY, or TURNSTILE_SECRET_KEY. It will not deploy until
# you have logged in yourself with your own Cloudflare account.
#
# Manual account actions (Cloudflare login, Turnstile site, Resend domain, Poe
# key) are described in README.md and echoed here where relevant.
#
# Usage:
#   ./deploy.sh            run the scripted steps
#   ./deploy.sh --dry-run  print every command without executing anything
set -eu

DB_NAME="eval-proxy-db"
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]
then
  DRY_RUN=1
  echo "DRY RUN: commands are printed only, nothing is executed."
  echo ""
fi

# Print a command, then run it unless this is a dry run.
run() {
  echo "+ $*"
  if [ "$DRY_RUN" -eq 0 ]
  then
    "$@"
  fi
}

echo "eval-proxy deploy helper"
echo "------------------------"
echo "This script runs only non-secret, scriptable steps. You set secrets and"
echo "log in yourself. See README.md for the full runbook."
echo ""

# --------------------------------------------------------------------------
# Step 1: confirm Cloudflare login. Never logs in on your behalf.
# --------------------------------------------------------------------------
echo "Step 1: check Cloudflare login (npx wrangler whoami)"
if [ "$DRY_RUN" -eq 0 ]
then
  WHOAMI=$(npx wrangler whoami 2>&1 || true)
  case "$WHOAMI" in
    *"not authenticated"*|*"not logged in"*|*"Please run"*|*"wrangler login"*)
      echo ""
      echo "You are not logged in to Cloudflare."
      echo "Log in with your own account, then re-run this script:"
      echo "  npx wrangler login"
      exit 1
      ;;
    *)
      echo "Logged in."
      ;;
  esac
else
  echo "+ npx wrangler whoami"
fi
echo ""

# --------------------------------------------------------------------------
# Step 2: install dependencies if needed.
# --------------------------------------------------------------------------
echo "Step 2: install dependencies if missing"
if [ ! -d node_modules ]
then
  run npm install
else
  echo "node_modules present, skipping npm install."
fi
echo ""

# --------------------------------------------------------------------------
# Step 3: create the D1 database only if it does not already exist.
# --------------------------------------------------------------------------
echo "Step 3: create D1 database '$DB_NAME' if missing"
if [ "$DRY_RUN" -eq 0 ]
then
  EXISTS=$(npx wrangler d1 list 2>/dev/null | grep -c "$DB_NAME" || true)
  if [ "$EXISTS" -eq 0 ]
  then
    run npx wrangler d1 create "$DB_NAME"
    echo ""
    echo "A database was created. Copy the printed database_id into wrangler.toml"
    echo "under [[d1_databases]], replacing REPLACE_WITH_D1_DATABASE_ID."
  else
    echo "Database '$DB_NAME' already exists, skipping create."
  fi
else
  echo "+ npx wrangler d1 list  (create '$DB_NAME' only if absent)"
fi
echo ""

# --------------------------------------------------------------------------
# Step 4: make sure wrangler.toml has a real database_id.
# --------------------------------------------------------------------------
echo "Step 4: check wrangler.toml database_id"
if grep -q "REPLACE_WITH_D1_DATABASE_ID" wrangler.toml
then
  echo ""
  echo "wrangler.toml still contains REPLACE_WITH_D1_DATABASE_ID."
  echo "Paste the database_id from the D1 create step into wrangler.toml under"
  echo "[[d1_databases]], then re-run this script."
  if [ "$DRY_RUN" -eq 0 ]
  then
    exit 1
  fi
  echo "(dry run: continuing to show the remaining steps)"
else
  echo "database_id looks set."
fi
echo ""

# --------------------------------------------------------------------------
# Step 5: apply the schema to the remote D1. Re-running is safe.
# --------------------------------------------------------------------------
echo "Step 5: apply schema.sql to the remote D1"
run npx wrangler d1 execute "$DB_NAME" --remote --file=./schema.sql
echo ""

# --------------------------------------------------------------------------
# Step 6: secrets. MANUAL. The script only prints the commands. It never reads
# or transmits any secret value.
# --------------------------------------------------------------------------
echo "Step 6: set your secrets yourself (MANUAL, not run by this script)"
echo "Run each command and paste the value when prompted:"
echo "  npx wrangler secret put POE_API_KEY"
echo "  npx wrangler secret put RESEND_API_KEY"
echo "  npx wrangler secret put TURNSTILE_SECRET_KEY"
echo ""

# --------------------------------------------------------------------------
# Step 7: deploy.
# --------------------------------------------------------------------------
echo "Step 7: deploy the Worker"
run npx wrangler deploy
echo ""

echo "Done."
echo "Next, point the frontend at your Worker:"
echo "  edit public/index.html and set WORKER_BASE_URL to the printed Worker URL"
echo "  (no trailing slash) and TURNSTILE_SITE_KEY to your Turnstile site key."
echo "If you set the secrets after the first deploy, run 'npx wrangler deploy'"
echo "once more so the running Worker picks them up."

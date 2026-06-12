#!/usr/bin/env bash
# One-command setup for gmail-mcp. Run from inside this folder:  ./setup.sh
# Installs the venv + locked deps, checks your config, runs one browser sign-in per
# account, registers the server with Claude Code, and verifies.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CFG_DIR="$HOME/.gmail-mcp-private"
CFG="${GMAIL_MCP_CONFIG:-$CFG_DIR/accounts.json}"

echo "==> 1/6  Python 3.10+"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "ERROR: python3 not found. Install Python 3.10+ first."; exit 1; }
"$PY" - <<'P'
import sys
assert sys.version_info >= (3, 10), f"Need Python 3.10+, have {sys.version.split()[0]}"
print("   ok:", sys.version.split()[0])
P

echo "==> 2/6  virtualenv + dependencies (hash-pinned)"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" -q install --upgrade pip
if "$HERE/.venv/bin/pip" -q install --require-hashes -r "$HERE/requirements.lock" 2>/dev/null; then
  echo "   installed from the hash-pinned lock"
else
  echo "   WARNING: the lock didn't match this platform — installing UNPINNED from requirements.txt"
  "$HERE/.venv/bin/pip" -q install -r "$HERE/requirements.txt"
fi

echo "==> 3/6  config (~/.gmail-mcp-private/accounts.json)"
mkdir -p "$CFG_DIR/tokens"; chmod 700 "$CFG_DIR" "$CFG_DIR/tokens"
if [ ! -f "$CFG" ]; then
  cp "$HERE/accounts.example.json" "$CFG"; chmod 600 "$CFG"
  echo "   ERROR: wrote a template to $CFG — edit it with YOUR aliases and addresses, then re-run."
  echo "   (Tip: open this folder in Claude Code and ask it to set the server up for you.)"
  exit 1
fi
chmod 600 "$CFG"
if grep -q "you@gmail.com\|you@yourcompany.com" "$CFG"; then
  echo "   ERROR: $CFG still contains the placeholder addresses — put your real accounts in, then re-run."
  exit 1
fi
echo "   accounts.json present"

echo "==> 4/6  OAuth client"
if [ ! -f "$CFG_DIR/oauth_client.json" ]; then
  cat <<EOF
   MISSING: $CFG_DIR/oauth_client.json
   Create a Desktop-app OAuth client (free, ~5 min) and put its JSON there:
     1. console.cloud.google.com -> create/pick a project
     2. enable the Gmail API
     3. OAuth consent screen: External -> fill name/email -> PUBLISH TO PRODUCTION
     4. Credentials -> Create credentials -> OAuth client ID -> type: Desktop app -> Download JSON
     5. mv ~/Downloads/client_secret_*.json $CFG_DIR/oauth_client.json
   Then re-run this script. (Full steps: README.md)
EOF
  exit 1
fi
chmod 600 "$CFG_DIR/oauth_client.json"
echo "   oauth_client.json present (0600)"

echo "==> 5/6  authorize each account (a browser opens per account — sign in to the MATCHING inbox)"
ALIASES="$("$HERE/.venv/bin/python" - <<P
import json, os
p = os.environ.get("GMAIL_MCP_CONFIG", os.path.expanduser("~/.gmail-mcp-private/accounts.json"))
print(" ".join(a["alias"] for a in json.load(open(p))["accounts"]))
P
)"
for a in $ALIASES; do
  echo "   --- $a ---"
  "$HERE/.venv/bin/python" "$HERE/authenticate.py" "$a" \
    || echo "   (failed/skipped for $a — re-run later: $HERE/.venv/bin/python $HERE/authenticate.py $a)"
done

echo "==> 6/6  register with Claude Code (user scope) + verify"
CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
"$CLAUDE" mcp add --scope user gmail-private -- "$HERE/.venv/bin/python" "$HERE/server.py" 2>&1 || true
"$HERE/.venv/bin/python" "$HERE/authenticate.py" --list

echo
echo "Done. Open a NEW Claude session and try:  list_accounts"
echo "Kill switch: claude mcp remove gmail-private   (or delete ~/.gmail-mcp-private)"

#!/usr/bin/env python3
"""Authorize one Gmail account for the MCP server. Run by a human — this is NOT an MCP tool,
so Claude can never trigger a sign-in.

Usage:
    python authenticate.py <alias>     # authorize the account with this alias (case-insensitive)
    python authenticate.py --list      # show configured accounts

It opens your browser; you sign in to the matching Google account and approve. The script
then verifies the signed-in address matches the alias's configured email (so an alias can't
bind to the wrong inbox) and saves the token, encrypted.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import auth
import gmail

CONFIG_PATH = pathlib.Path(
    os.environ.get("GMAIL_MCP_CONFIG", os.path.expanduser("~/.gmail-mcp-private/accounts.json"))
)


def _config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Create it from accounts.example.json.")
    auth.harden_file(CONFIG_PATH)
    cfg = json.loads(CONFIG_PATH.read_text())
    auth.validate_accounts(cfg.get("accounts", []))
    return cfg


def main(argv: list[str]) -> int:
    cfg = _config()
    by_alias = {a["alias"].lower(): a for a in cfg.get("accounts", [])}

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        for a in cfg.get("accounts", []):
            print(f"{a['alias']:12} {a['email']:30} scope={a.get('scope', 'modify')}")
        return 0

    a = by_alias.get(argv[0].lower())
    if not a:
        names = ", ".join(x["alias"] for x in cfg.get("accounts", []))
        sys.exit(f"Unknown alias '{argv[0]}'. Configured: {names}")
    alias = a["alias"]  # canonical case

    scopes = auth.SCOPES.get(a.get("scope", "modify"), auth.SCOPES["modify"])
    print(f"Authorizing '{alias}' ({a['email']}) with scope '{a.get('scope', 'modify')}' ...")
    print("A browser window will open — sign in to THAT account and approve.")

    creds = auth.authorize_interactive(alias, scopes)

    signed_in = gmail.get_profile_email(creds).lower()
    if signed_in != a["email"].lower():
        sys.exit(
            f"\nABORTED: you signed in as '{signed_in}', but alias '{alias}' expects "
            f"'{a['email']}'. Nothing was saved — re-run and pick the right account."
        )

    auth.save_credentials(alias, creds)
    print(f"\n✓ Authorized and saved (encrypted) for '{alias}' <{signed_in}>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

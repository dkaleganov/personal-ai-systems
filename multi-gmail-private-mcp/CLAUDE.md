# CLAUDE.md — setup brief for Claude Code

This repo is a self-hosted, multi-account Gmail MCP server (see README.md). When the user
asks you to set it up, you run the install end to end. The user does only the two things a
human must do: create the OAuth client in Google Cloud Console, and complete each browser
sign-in. Everything below is your playbook.

## Hard rules (do not violate)

- **Never read, print, or copy secrets.** That means `~/.gmail-mcp-private/oauth_client.json`,
  anything in `~/.gmail-mcp-private/tokens/`, and any `client_secret*.json`. You may check
  these files exist and fix their permissions (chmod 600); never `cat` them.
- **Never commit or move secrets into this repo.** The runtime config lives outside the
  repo at `~/.gmail-mcp-private/` and is gitignored anyway.
- **Never run `authenticate.py` without telling the user a browser sign-in is coming**, and
  never try to complete a sign-in yourself — the user clicks Allow.
- **Don't enable sending.** `enable_send` stays false unless the user explicitly asks,
  understands it also needs `GMAIL_MCP_ALLOW_SEND=1` in the environment, and confirms.

## Setup playbook

1. **Python check:** `python3 --version` must be 3.10+.

2. **Accounts config.** If `~/.gmail-mcp-private/accounts.json` is missing or still has the
   placeholder addresses (`you@gmail.com`), ask the user which Gmail/Workspace accounts to
   connect: a short alias + the exact email for each. Then write the file (mode 600, dir
   700) based on `accounts.example.json`. Scope `modify` is the right default; `readonly`
   for accounts the user only wants to read. Keep `enable_send: false`.

3. **OAuth client.** Check whether `~/.gmail-mcp-private/oauth_client.json` exists. If not,
   walk the user through creating it (they do this in their browser, ~5 minutes):
   - console.cloud.google.com → create (or pick) a project
   - APIs & Services → Library → enable the **Gmail API**
   - OAuth consent screen → External → fill app name + support email → **Publish to
     production** (otherwise refresh tokens expire after 7 days)
   - Credentials → Create credentials → OAuth client ID → type **Desktop app** → Download JSON
   - Then you (Claude) move the downloaded file:
     `mkdir -p ~/.gmail-mcp-private && mv ~/Downloads/client_secret_*.json
     ~/.gmail-mcp-private/oauth_client.json && chmod 600 ~/.gmail-mcp-private/oauth_client.json`
     and delete any stray copies left in Downloads after confirming with the user.

4. **Install:** run `./setup.sh` from the repo root, or do its steps yourself:
   venv at `.venv`, then `pip install --require-hashes -r requirements.lock` (fall back to
   `pip install -r requirements.txt` only if the lock doesn't match the platform — say so).

5. **Authorize each account, one at a time:**
   `.venv/bin/python authenticate.py <alias>` — tell the user which account to pick in the
   browser. The "Google hasn't verified this app" screen is expected (it's their own app):
   Advanced → Continue. If the user signs into the wrong account, the script aborts safely
   and nothing is saved — just re-run. For Workspace accounts blocked by org policy, the
   admin must allow the client (Admin console → Security → API controls → App access control).

6. **Register with Claude Code (user scope, absolute paths):**
   `claude mcp add --scope user multi-gmail-private-mcp -- "$PWD/.venv/bin/python" "$PWD/server.py"`

7. **Verify:** `.venv/bin/python authenticate.py --list` shows every alias; then tell the
   user to open a NEW Claude session and ask for `list_accounts` — every account should
   read "authorized".

## Troubleshooting

- **"Identity mismatch" / aborted sign-in** — the safety guard working; the wrong Google
  account was picked in the browser. Re-run that alias.
- **Token "can't be decrypted" mentioning the Keychain** — the macOS Keychain is locked in
  this session (common over SSH). Retry from a logged-in GUI session.
- **`invalid_client`** — the `oauth_client.json` is malformed or the wrong type; it must be
  a **Desktop app** client.
- **Workspace sign-in blocked** — org policy; see the admin step above.
- **Kill switch** — `claude mcp remove multi-gmail-private-mcp`; revoke at
  myaccount.google.com/permissions; delete `~/.gmail-mcp-private/` to remove everything.

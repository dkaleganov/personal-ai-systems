# gmail-mcp — private, multi-account Gmail for Claude

A small, self-hosted MCP server that gives Claude **read + triage + draft** access to
**all of your Gmail / Google Workspace accounts** — as many as you want, in one connector.

Built because Claude's official Gmail connector supports exactly **one** Google account
per Claude account. Built **privacy-first**: it runs on your machine, your mail talks only
to Google's API, and no third-party service ever sits in the middle.

```
   you, on any device
         |
       Claude            (desktop app / Claude Code)
         |  MCP · stdio · local-only
         v
   +-----------------------------------------------+
   |  gmail-mcp  (Python, ~900 lines, this repo)   |
   |  search · read · label · archive · draft      |
   |  NO send by default · NO delete, ever         |
   |  per-account encrypted tokens · audit log     |
   +-----------------------------------------------+
         |  one OAuth token per account (Fernet-encrypted,
         |  key in the macOS Keychain)
         v
   account-1   account-2   account-3   ...
         \         |         /
          Gmail API (scope: gmail.modify — cannot permanently delete)
```

## What Claude can do — and deliberately can't

| Can (per account) | Can't |
|---|---|
| Search with full Gmail query syntax | **Send** — no send tool is even registered (see below) |
| Read messages and full threads | **Delete** — no delete tool; the OAuth scope cannot permanently delete; adding `TRASH`/`SPAM` labels is refused (Trash auto-purges after ~30 days, which would be deletion on a delay) |
| List labels; add/remove labels (archive = remove `INBOX`, mark read = remove `UNREAD`) | Start an OAuth sign-in — authorization is a CLI only a human runs |
| Create **drafts** (you review and hit send yourself in Gmail) | Touch any account you didn't explicitly authorize |

**Arming send is two-factor by design:** Google has no OAuth scope that allows drafts
without send, so "drafts-only" can only be enforced at the app layer. This server therefore
requires BOTH `"enable_send": true` in the config file AND `GMAIL_MCP_ALLOW_SEND=1` in the
server's environment before a send tool exists. A config edit alone — accidental or
injected — can never enable sending. Leave both off; drafts are the safe default.

## Security model (the short version)

- **Official libraries only** — Anthropic's `mcp` SDK, Google's API/auth clients,
  `cryptography`, `keyring`. Zero third-party MCP code to vet or get poisoned through.
- **Per-account encrypted tokens** — Fernet (authenticated encryption); the key lives in
  the macOS Keychain, with an atomic `0600` key-file fallback. Decryption tries every
  reachable key (MultiFernet), so a locked-Keychain SSH session can't strand your tokens.
- **Strict account isolation** — every tool call names an account; the server re-verifies
  the token's *actual* signed-in identity against your config before use. During setup,
  signing in to the wrong account is detected and rejected.
- **Prompt-injection fencing** — email content returns wrapped in an UNTRUSTED envelope
  with a per-call random nonce; marker-forgery and header CR/LF injection are stripped.
  Treated as defense-in-depth, not a guarantee — drafts-only + no-delete are the backstop.
- **Audit log, metadata only** — every call appends one JSON line (tool, account alias,
  ids, counts) to `~/.gmail-mcp-private/audit.log` (`0600`, rotating). Never content.
- **Local only** — stdio transport; the server never opens a network port.
- **Supply chain pinned** — `requirements.lock` is hash-pinned (`pip --require-hashes`).

Full threat model, guarantees, and residual risks: [SECURITY.md](SECURITY.md).

## Requirements

- macOS with Python 3.10+ (Linux should work via `keyring`'s Secret Service backend or the
  key-file fallback, but is untested). Disk encryption (FileVault) strongly recommended.
- A free Google Cloud project (you create it once; instructions below).
- [Claude Code](https://claude.com/claude-code) or the Claude desktop app as the client.

## Setup — the AI-assisted way (recommended, ~15 minutes)

This repo ships a [CLAUDE.md](CLAUDE.md) that briefs Claude Code on the entire setup. So:

```bash
git clone https://github.com/dkaleganov/personal-ai-systems
cd personal-ai-systems/gmail-mcp
claude
```

Then say:

> Set up this Gmail MCP server for my accounts.

Claude Code walks you through everything: the Google Cloud project, the OAuth client, your
`accounts.json`, the venv and locked dependencies, each browser sign-in, registration, and
verification. **You** do the parts only a human should: creating credentials in the Google
console, and clicking *Allow* once per account. Claude never sees a secret — tokens are
written encrypted by the auth CLI, and the OAuth client JSON is a file you download yourself.

## Setup — the manual way

1. **Google Cloud (once):** create a project at console.cloud.google.com → enable the
   **Gmail API** → configure the **OAuth consent screen** (External; fill in the app name
   and your email; then **Publish to production** — otherwise refresh tokens expire every
   7 days) → **Credentials → Create credentials → OAuth client ID → Application type:
   Desktop app** → **Download JSON**.
2. **Place the client file:**
   ```bash
   mkdir -p ~/.gmail-mcp-private
   mv ~/Downloads/client_secret_*.json ~/.gmail-mcp-private/oauth_client.json
   chmod 600 ~/.gmail-mcp-private/oauth_client.json
   ```
3. **Configure your accounts:** copy `accounts.example.json` to
   `~/.gmail-mcp-private/accounts.json` and put in your real aliases + addresses.
4. **Run the setup script:**
   ```bash
   ./setup.sh
   ```
   It builds a venv, installs the hash-pinned dependencies, then opens a browser sign-in
   for each configured account (pick the matching inbox; on the "Google hasn't verified
   this app" screen — it's *your* app — click **Advanced → Continue**), registers the
   server with Claude Code, and verifies.
5. **Test:** in a new Claude session, ask for `list_accounts`.

### Google Workspace accounts

If an account is on Google Workspace, the admin may need to allow the app: Admin console →
Security → API controls → App access control → mark your OAuth client trusted. Trade-off:
that trusts the client org-wide, so guard your `oauth_client.json`.

## Using it

Every tool takes an `account` (your alias or the full address). Examples to ask Claude:

- "Search **work** for unread invoices from the last week."
- "Read that thread and draft a reply saying we'll confirm by Friday."
- "Archive everything from that newsletter in **personal**."
- "What labels do I have on **side**?"

Drafts land in the account's Drafts folder — you review and send them from Gmail yourself.

## More machines

Each install is fully independent, with its own encrypted tokens: repeat the setup on a
laptop and the two never conflict. Your phone can use it by remote-driving a machine that's
always on (e.g. Claude on a Mac mini over Tailscale + SSH) — the server itself stays local
and never opens a port.

## Kill switch

- **Disable:** `claude mcp remove gmail-private`
- **Revoke one account:** delete `~/.gmail-mcp-private/tokens/<alias>.enc` and/or revoke
  the app at <https://myaccount.google.com/permissions>
- **Remove everything:** delete `~/.gmail-mcp-private/`

## Files to audit (please do — it's ~900 lines)

| File | What it is |
|---|---|
| `server.py` | the MCP server + the exact tools Claude can call |
| `gmail.py` | Gmail API wrapper, MIME parsing, label resolution, header-injection guards |
| `auth.py` | OAuth loopback flow, encrypted/atomic token storage, account validation |
| `authenticate.py` | the sign-in CLI a human runs (never an MCP tool) |
| `auditlog.py` | metadata-only audit logging (0600, rotating) |
| `setup.sh` | one-command install |
| `CLAUDE.md` | the brief that lets Claude Code run your setup |

## License & author

MIT — see [LICENSE](LICENSE). Built by [Dima Kaleganov](https://github.com/dkaleganov)
with Claude Code, as part of [personal-ai-systems](https://github.com/dkaleganov/personal-ai-systems).

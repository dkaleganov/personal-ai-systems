# mercury-multiorg-mcp

Read-only [MCP](https://modelcontextprotocol.io) server that exposes **several
Mercury organizations to one AI session**. Mercury's hosted MCP and its API
tokens are single-organization per connection; this server holds one
read-only token per org and routes every tool call by an explicit `entity`
key.

Status: **Phase 1** (core tools). See `CLAUDE.md` for the full build brief
and the later phases (1099 cross-check, keepalive CLI, holistic read surface).

## Security model

- **Read-only.** Only `GET` endpoints have client methods; the package has no
  code path that can move money, edit recipients, or change anything.
- **Stdio only.** The server never opens a network listener.
- **Explicit entity, always.** Every tool that touches Mercury takes an
  `entity` argument. There is no default. Every result carries the entity it
  came from.
- **Tokens stay in the environment.** The registry names an env var per org;
  the server reads that env var and nothing else. Errors and logs never
  contain more than the last four characters of a token.
- **Untrusted output.** Transaction memos, counterparty names, bank
  descriptions, and filenames are third-party text returned verbatim. Clients
  and models must treat tool output as data, never as instructions.
- **Reduced identifiers.** `list_accounts` masks account numbers to the last
  four digits and omits routing numbers; `list_transactions` omits
  counterparty bank details.

## Install

From a clone:

```bash
cd mercury-multiorg-mcp
uv sync
uv run mercury-multiorg-mcp --entities /private/path/entities.yaml
```

Or pin a full commit SHA with `uvx` (pin a SHA, not a tag: `@tag` +
`#subdirectory=` has had resolver bugs in uv, and SHAs are cache-safe):

```bash
uvx --from 'git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp' \
  mercury-multiorg-mcp --entities /private/path/entities.yaml
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Configure

1. In each Mercury org: org switcher → All Settings → Tokens → create a
   **Read Only** token (no IP allowlist required).
2. Copy `entities.example.yaml` to a private location outside this repo and
   list your orgs (`key`, `display_name`, `token_env`).
3. Export one env var per org, named as in the registry, in the environment
   that launches the server. `.env.example` shows the names; a `.env` file
   next to the launch directory is loaded if present (never committed).
4. Optional: `MERCURY_API_BASE=https://api-sandbox.mercury.com` with
   sandbox-created tokens.

Mercury deletes tokens unused for 45 days and downgrades unused permissions on
the same clock; a keepalive CLI ships in Phase 2.

## Register in Claude Code

`.mcp.json` in this folder registers the server against the example registry
(no tokens, so `list_accounts` returns a clean per-entity error). For real use,
copy the block into your private project's `.mcp.json` and point `--entities`
at your private registry:

```json
{
  "mcpServers": {
    "mercury-multiorg": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp",
        "mercury-multiorg-mcp",
        "--entities",
        "/private/path/entities.yaml"
      ],
      "env": {
        "MERCURY_TOKEN_ACME_MAIN": "${MERCURY_TOKEN_ACME_MAIN}"
      }
    }
  }
}
```

## Tools (Phase 1)

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_entities` | — | entity keys, display names, whether each token env var is set |
| `list_accounts` | `entity` | accounts with `availableBalance` / `currentBalance` |
| `list_transactions` | `entity`, `account_id?`, `start?`, `end?`, `search?`, `limit=100` | newest-first transactions, `truncated` flag |
| `server_info` | — | package version, API base, entity count (no secrets) |

`start` / `end` filter on `createdAt` (`YYYY-MM-DD` or ISO 8601). The Mercury
dashboard displays `postedAt`, so a date range may differ slightly from the UI.

## Develop

```bash
uv sync
uv run pytest
```

Tests use synthetic JSON fixtures and a mock HTTP transport only. Nothing in
this package, its tests, or its history may contain real names, tokens, or
account identifiers.

## License

MIT (the monorepo `LICENSE` applies).

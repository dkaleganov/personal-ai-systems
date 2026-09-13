# Keepalive

Mercury automatically **deletes any API token that goes unused for 45
days** and **downgrades permissions unused for 45 days** (an email notice
goes to org admins seven days before either). Source:
[API token security policies](https://docs.mercury.com/docs/api-token-security-policies).
Any authenticated call resets the clock, so a read-only server that is
only used occasionally can lose its tokens without anyone noticing.

`mercury-multiorg-mcp-keepalive` makes one authenticated `GET /accounts`
per configured organization and prints one line per entity. It is a CLI
for cron or launchd, not an MCP tool, and it never prints a token.

```bash
# from a clone
uv run mercury-multiorg-mcp-keepalive --entities /private/path/entities.yaml

# equivalent module form
uv run python -m mercury_multiorg_mcp.keepalive --entities /private/path/entities.yaml

# pinned install (replace the SHA)
uvx --from 'git+https://github.com/dkaleganov/personal-ai-systems@<FULL_COMMIT_SHA>#subdirectory=mercury-multiorg-mcp' \
  mercury-multiorg-mcp-keepalive --entities /private/path/entities.yaml
```

Same configuration conventions as the server: `--entities PATH` or
`MERCURY_ENTITIES_FILE`, optional `--env-file PATH` (only then is a dotenv
file read; existing env vars win), optional `--api-base URL` or
`MERCURY_API_BASE` (production, `https://api-sandbox.mercury.com`, or a
loopback mock; any other host needs `--allow-custom-api-base` on the
command line). A configured token without the documented `secret-token:`
prefix produces a warning line on stderr (last four characters only).

## Output

One line per entity, in registry order, to stdout:

```
2026-09-12T17:00:00Z OK acme_main HTTP 200
2026-09-12T17:00:01Z FAIL acme_ops no token configured (MERCURY_TOKEN_ACME_OPS unset)
2026-09-12T17:00:02Z FAIL acme_main HTTP 401
```

Exit status:

| Code | Meaning |
| --- | --- |
| 0 | every configured entity succeeded |
| 1 | at least one entity failed, or no entity has a token configured at all |
| 2 | configuration error (registry missing, invalid, or unreadable; bad or disallowed API base); always one line on stderr, never a traceback |

Alert on a nonzero exit or on any `FAIL` line: an HTTP 401 usually means
the token was already deleted or the env var holds a stale value.

## Cadence

Weekly is plenty: it leaves six retries inside the 45-day window if the
job host is down or the scheduler misses runs. Do not go longer than a
couple of weeks.

## cron

A crontab entry must be one physical line (crontab has no backslash
continuation, and a literal `%` is turned into a newline). Every Monday
at 09:15 local time; adjust the placeholder paths and keep the registry
and token env vars outside any repository:

```cron
15 9 * * 1 /path/to/venv/bin/mercury-multiorg-mcp-keepalive --entities /private/path/entities.yaml --env-file /private/path/mercury.env >> /path/to/logs/mercury-keepalive.log 2>&1
```

If the line gets unwieldy, point cron at a one-line wrapper script
instead (`15 9 * * 1 /path/to/bin/mercury-keepalive.sh`) and put the
flags in the script. If your tokens come from a secret manager rather
than a dotenv file, wrap the command in whatever that tool provides to
inject env vars for one process; the CLI only reads the environment.

## launchd (macOS)

Save as `~/Library/LaunchAgents/com.example.mercury-keepalive.plist`, then
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.mercury-keepalive.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.mercury-keepalive</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/mercury-multiorg-mcp-keepalive</string>
    <string>--entities</string>
    <string>/private/path/entities.yaml</string>
    <string>--env-file</string>
    <string>/private/path/mercury.env</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>1</integer>
    <key>Hour</key>
    <integer>9</integer>
    <key>Minute</key>
    <integer>15</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/path/to/logs/mercury-keepalive.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/logs/mercury-keepalive.log</string>
</dict>
</plist>
```

launchd runs a missed `StartCalendarInterval` job at the next wake if the
machine was asleep, but not if it was powered off; check the log after an
outage.

## Verifying

```bash
mercury-multiorg-mcp-keepalive --entities /private/path/entities.yaml; echo "exit=$?"
```

You should see one `OK` line per entity and `exit=0`. A token's last-used
time is visible in each org's dashboard under All Settings, Tokens.

# AGENTS.md

The build brief and history are in
[CLAUDE.md](https://github.com/dkaleganov/personal-ai-systems/blob/main/mercury-multiorg-mcp/CLAUDE.md).
The filename is historical; its requirements apply to any agent working on
this package. Read it before changing code here. In short:

- Public repository from the first commit: fake data only (`acme_main`,
  `acme_ops` style) in examples, fixtures, and tests; no real names, tokens,
  or account identifiers anywhere, including commit messages.
- Read-only: only `GET` endpoints get client methods, ever. Stdio transport only.
- Call `list_entities` to discover entity keys. Every tool that accesses
  Mercury requires an explicit `entity` and identifies it in its successful
  result. `list_entities` and `server_info` require no entity argument.
- Live Mercury docs (https://docs.mercury.com/llms.txt) beat the brief.
- Run `gitleaks git --no-banner --redact .` from the monorepo root before
  declaring any change done.

This is an MCP server for clients that support local stdio (see README.md).
The `.mcp.json` here is an example project-discovery configuration; client
discovery rules vary.

# AGENTS.md

Build brief and ground rules for this package live in **[CLAUDE.md](CLAUDE.md)**
(the file name is historical; the content is client-neutral). Read it before
changing code here. In short:

- Public repository from the first commit: fake data only (`acme_main`,
  `acme_ops` style) in examples, fixtures, and tests; no real names, tokens,
  or account identifiers anywhere, including commit messages.
- Read-only: only `GET` endpoints get client methods, ever. Stdio transport only.
- Every tool takes an explicit `entity`; every result carries it back.
- Live Mercury docs (https://docs.mercury.com/llms.txt) beat the brief.
- Run `gitleaks git --no-banner --redact .` from the monorepo root before
  declaring any change done.

This is an MCP server for any MCP client (see "Works with any MCP client" in
[README.md](README.md)); the `.mcp.json` here is one client's convention.

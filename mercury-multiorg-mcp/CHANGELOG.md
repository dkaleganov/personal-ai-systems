# Changelog

## 0.1.1 (2026-09-13)

Fixes from an independent external review of 0.1.0 (six majors, four
minors, plus threat-model hardening). No new endpoints; the tool surface
shrinks by two tools unless `--allow-documents` is passed.

### Majors

- **Tool-error boundary (M1).** Every tool error is now fixed text: an HTTP
  status, an endpoint label with ids replaced by `{id}`, and a hint from a
  table. Upstream response bodies are never read on an error status, header
  values (including `Content-Type`) and httpx exception reprs are never
  quoted, and caller-supplied ids and other arguments are never echoed
  ("invalid id format"). Every message is scrubbed for the entity's own
  token value at the boundary, independent of the logging filter.
- **Webhook receiver URLs (M2).** `list_webhooks` no longer returns `url`
  or `path_fingerprint`: the hostname itself can be the capability. It
  returns `url_fingerprint` (first 8 hex chars of sha256 of the full URL)
  with id, status, `enabled`, event types, filter paths, and timestamps.
- **Documents are opt-in (M3).** `get_statement_pdf` and `get_invoice_pdf`
  are registered only with `--allow-documents` or
  `MERCURY_ALLOW_DOCUMENTS=1` (24 tools by default, 26 with the flag);
  `server_info` reports `documents_enabled` and the PDF metadata block
  carries `redacted: false`. A download must arrive as `application/pdf`
  or `application/octet-stream`, start with `%PDF-`, and carry a `%%EOF`
  marker in its last 2 KB; anything else is a clean error without echo.
- **Byte caps on wire bytes (M4).** Every request sends
  `Accept-Encoding: identity`; a response with any other
  `Content-Encoding` is refused before its body is read; the 10 MB PDF cap
  and a new 32 MB JSON cap are enforced on raw wire bytes while streaming.
  A 32 KB gzip body that expanded to 32 MB (peaking near 81 MB) is now
  rejected on the header alone.
- **Windowed feeds walk in full (M5).** `since` on `list_events` and
  `start`/`end` on `list_treasury_transactions` no longer early-stop on
  the first out-of-window row. The whole bounded feed is walked, filtered,
  and sorted newest first before `limit` applies, so `truncated` is exact.
  `order_verified` is gone.
- **Stalled pagination fails loudly (M6).** In every cursor walk, a page
  that contributes no fresh rows, yields no usable cursor, or does not
  advance the cursor while the API still advertises more raises
  `IncompletePaginationError` (a `MercuryAPIError`). `reportable_totals`
  can no longer return a total built on a stalled walk.

### Minors

- **Allowlists at every level (m1).** Projection is recursive with explicit
  sub-allowlists for the eleven nested shapes (transaction merchant,
  category data, currency exchange info; organization DBAs; treasury net
  returns, dividends, and transaction details; card spend limit, budgets,
  merchant lock; invoice line items) and for event patches. Unknown nested
  keys are dropped; a canary test plants them and asserts none survive.
- **Threshold validation (m2).** `reportable_totals` rejects a non-finite,
  negative, or > 1,000,000,000 threshold with a fixed error; an
  unrepresentable value is never coerced to zero.
- **History note (m3).** The README hygiene statement records that the
  first Phase 1 commit's fixtures used a real, public ABA routing number as
  sample data (replaced in the next commit; no history rewrite).
- **Startup errors (m4).** A registry with a non-string top-level key, a
  non-mapping entry, an unreadable or non-UTF-8 file, or a bad `--env-file`
  exits 2 with one line on stderr for both CLIs; the redacting exception
  hooks are installed before the registry loads.

### Hardening

- `token_env` must match `^MERCURY_TOKEN_[A-Z0-9_]+$`; a registry cannot
  name an arbitrary environment variable.
- `--api-base` / `MERCURY_API_BASE` is restricted to
  `https://api.mercury.com`, `https://api-sandbox.mercury.com`, and
  loopback unless `--allow-custom-api-base` is passed on the command line
  (server and keepalive).
- Both CLIs warn at startup, per entity, when a configured token does not
  carry the documented `secret-token:` prefix (last four characters only).
- The README links to CLAUDE.md on GitHub (the sdist does not ship it).

### Docs

README security model rewritten around four classes of output
(structured fields, tool errors, free-text fields, documents);
docs/tools.md updated for every changed tool; CLAUDE.md brief updated.

### Re-validation fixes (second commit, same version)

The reviewer re-validated the first 0.1.1 commit: eight of ten findings
closed, two partial (M1, m1), two new majors, four minors, and several
documentation corrections. All addressed before release:

- **B1 (regression, major).** A missing or non-object `page`, or a
  `nextPage` that is neither null nor an id, is `malformed pagination
  metadata` (an `IncompletePaginationError`), never "the last page".
  Terminal null cursors and empty final pages still complete normally.
- **B2 (major).** Rows are deduplicated as each one is accepted, so a row
  repeated inside a single page is dropped once (it was counted twice);
  exact duplicates are counted in `duplicates_dropped` on every paginated
  result and in `reportable_totals.totals` (with
  `recipient_duplicates_dropped`); the same id with different content is a
  `conflicting duplicate rows` error.
- **B3 (M1 partial).** The server subclasses the SDK's `MCPServer` and
  overrides its public `call_tool` so an argument-validation failure is
  rendered as field path and expected type only (`year: expected an
  integer (int_parsing)`); the SDK's own text, which quoted the caller's
  value (including a pasted token), never reaches the client. A test pins
  the SDK behaviour this relies on. A non-identifier tool name is no
  longer echoed in "Unknown tool".
- **B4 (m1 partial).** Derived outputs are scalar-projected: `list_tax_docs`
  joins names and statuses from projected recipient objects,
  `reportable_totals.display_name` uses the recipient name only when it is
  a string, `unclassified` rows and sample transaction ids null any value
  that arrives as an object or array.
- **B5.** A negative treasury `cursor` is rejected (schema minimum 0); zero
  and decreasing non-negative cursors still work.
- **B6.** `token_env` and entity keys are validated with `fullmatch`, so a
  trailing newline is rejected.
- **B7.** A malformed registry YAML is one stderr line with the parser's
  problem and line/column, exit 2, on both CLIs; no source snippet.
- **B8.** The PDF envelope check ignores trailing PDF whitespace before
  looking for `%%EOF` in the last 2 KiB and is documented as an envelope
  check, not parsing; the bytes are returned as received.
- **Docs.** Unwindowed events and treasury results are the API's `desc`
  order, chronological order is guaranteed only for windowed calls; the
  caller's invoice id appears in `get_invoice_pdf` success metadata and
  URI (only the slug stays internal); the monorepo README qualifies the
  masking claim for opt-in PDFs; the routing-number fixture was replaced
  in the next commit; literal known-token scrubbing applies to values of
  8+ characters.
- **Vendor neutrality.** README gains "Works with any MCP client" with
  configuration for Claude Code, Claude Desktop, Codex CLI, Cursor /
  Windsurf / VS Code, Gemini CLI, and any stdio MCP client; `AGENTS.md`
  at the package root points to `CLAUDE.md` as the build brief; server
  instructions and tool descriptions are client-neutral.

## 0.1.0 (2026-09-12)

Initial release: Phases 1-4 (core, 1099 cross-check, holistic read
surface, release pass). See the tag `mercury-v0.1.0`.

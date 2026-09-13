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
  sample data (replaced two commits later; no history rewrite).
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

## 0.1.0 (2026-09-12)

Initial release: Phases 1-4 (core, 1099 cross-check, holistic read
surface, release pass). See the tag `mercury-v0.1.0`.

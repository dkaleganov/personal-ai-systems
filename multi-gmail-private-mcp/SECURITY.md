# Security model

This server exists so an AI assistant can triage email **without** a third-party service in
the middle and **without** the ability to do irreversible damage. Every guarantee below
names its enforcement layer, because that's the honest way to state guarantees.

## Guarantees and how they're enforced

| Guarantee | Enforced by |
|---|---|
| No third party sees your mail | Architecture: the server runs on your machine; the only network calls are to `https://www.googleapis.com` (Gmail). No telemetry. |
| Cannot permanently delete | OAuth scope (`gmail.modify` cannot hard-delete) AND no delete tool exists AND adding `TRASH`/`SPAM` labels is refused (Trash auto-purges after ~30 days = delayed deletion). |
| Cannot send (by default) | App layer, twice: no send tool is registered unless BOTH `"enable_send": true` (config) AND `GMAIL_MCP_ALLOW_SEND=1` (environment) are set. Transparency: Google has **no** scope that permits drafts but not send, so scope-level enforcement is impossible — that's exactly why the gate is two-factor. |
| Cannot touch the wrong account | Explicit `account` on every tool; aliases/emails validated unique; sign-in CLI refuses a token whose real identity doesn't match the configured address; the server re-verifies token identity at runtime before first use. |
| Tokens unreadable at rest | Fernet (authenticated encryption), key in the macOS Keychain with an atomic `0600` file fallback; `0700` config dir, `0600` files, atomic writes. Decryption tries every reachable key (MultiFernet), so sessions where the Keychain is locked can't strand tokens written elsewhere. |
| Email can't easily hijack the assistant | Untrusted-content fencing: per-call random nonce envelope, marker-forgery stripping, CR/LF header sanitization. This is defense-in-depth, **not** a guarantee — the hard backstop is drafts-only + no-delete. |
| Tamper-evident usage | Append-only audit log, one JSON line per call, metadata only (tool, alias, ids, counts — never content, queries, subjects, or recipients), `0600` including rotated files. |
| Dependencies can't drift | `requirements.lock` is hash-pinned; install with `--require-hashes`. Only official Anthropic/Google/PyPA-mainstream libraries. |
| AI can't self-authorize | OAuth sign-in lives in `authenticate.py`, a CLI a human runs. The server can only read/refresh tokens that already exist. |

## Residual risks (accept these knowingly)

- **Same-user malware.** Anything running as your OS user can read the Keychain entry the
  same way the server does and decrypt tokens. True of every local credential store
  (`gh`, `gcloud`, AWS CLI). Mitigations: OS user separation, disk encryption, don't run
  untrusted code.
- **Draft exfiltration needs your finger.** A prompt-injected assistant could create a
  draft addressed to an attacker containing sensitive content. Nothing leaves unless you
  press send in Gmail — so glance at recipient and content before sending drafts you
  didn't ask for.
- **Fencing is best-effort.** Treat returned email text as data. The architecture assumes
  fencing will sometimes fail and limits what a hijacked session could do instead.
- **Arming send is a local decision.** The two-factor gate stops accidents and config-only
  tampering; it cannot stop the machine's owner. Protect the machine.
- **Workspace trust is org-wide.** If a Workspace admin marks the OAuth client trusted,
  guard `oauth_client.json` accordingly.

## Reporting

Found something? Open an issue (or a private security advisory) on the repository.

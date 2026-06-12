"""OAuth + encrypted token storage for the multi-account Gmail MCP server.

Security properties:
- Tokens are encrypted at rest with Fernet (authenticated AES).
- The Fernet key lives in the macOS Keychain when available, else a key file created
  ATOMICALLY at 0600 (and if it ever falls back to the file, a loud warning is printed).
- Decryption tries EVERY reachable key (Keychain + key file, via MultiFernet), so a
  session where one source is unreachable (locked Keychain over SSH, pre-login launch)
  can never strand tokens written under the other key. Encryption always uses the
  Keychain-first primary key, so tokens migrate back to the Keychain key over time.
- The config dir is 0700; token files are 0600 and written atomically (tmp + os.replace).
- Account aliases are strictly validated ([A-Za-z0-9_-]+); aliases and emails must be unique.
- OAuth uses the Desktop "installed app" loopback flow.
- Interactive authorization happens ONLY in authenticate.py. The MCP server only ever
  reads/refreshes existing tokens; it can never start a sign-in.

Requires Python 3.10+.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import sys
import tempfile

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError

# Least-privilege scope sets. 'modify' covers read + label/archive + draft and CANNOT
# permanently delete. 'readonly' is read-only. (We never request gmail.send, gmail.compose,
# or https://mail.google.com/.)
SCOPES = {
    "readonly": ["https://www.googleapis.com/auth/gmail.readonly"],
    "modify": ["https://www.googleapis.com/auth/gmail.modify"],
}

CONFIG_DIR = pathlib.Path(os.path.expanduser("~/.gmail-mcp-private"))
TOKENS_DIR = CONFIG_DIR / "tokens"
KEY_FILE = CONFIG_DIR / "fernet.key"
OAUTH_CLIENT_FILE = CONFIG_DIR / "oauth_client.json"  # Desktop client JSON from Google Cloud

KEYRING_SERVICE = "gmail-mcp-private"
KEYRING_KEY_NAME = "fernet-key"

ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_alias(alias: str) -> str:
    """Aliases must be [A-Za-z0-9_-]+ so they map 1:1 to a token filename (no collisions)."""
    if not isinstance(alias, str) or not ALIAS_RE.match(alias):
        raise ValueError(
            f"Invalid account alias {alias!r}: only letters, digits, '-' and '_' are allowed."
        )
    return alias


def validate_accounts(accounts: list[dict]) -> None:
    """Validate every alias and require aliases + emails to be unique (case-insensitive),
    so the alias/email lookup map can't collide onto the wrong account."""
    seen_alias, seen_email = set(), set()
    for a in accounts:
        al = validate_alias(a["alias"]).lower()
        em = (a.get("email") or "").lower()
        if not em:
            raise ValueError(f"Account '{a['alias']}' is missing an email.")
        if al in seen_alias:
            raise ValueError(f"Duplicate account alias: {a['alias']}")
        if em in seen_email:
            raise ValueError(f"Duplicate account email: {a['email']}")
        seen_alias.add(al)
        seen_email.add(em)


def harden_file(path: pathlib.Path) -> None:
    """Best-effort chmod 600 on a sensitive file; warn loudly if it can't be restricted."""
    try:
        if path.exists():
            os.chmod(path, 0o600)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                print(
                    f"[gmail-mcp-private] WARNING: {path} is {oct(mode)} — could not restrict to 0600.",
                    file=sys.stderr,
                )
    except OSError as e:
        print(f"[gmail-mcp-private] WARNING: could not chmod {path}: {e}", file=sys.stderr)


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKENS_DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    os.chmod(TOKENS_DIR, 0o700)


class TokenDecryptError(RuntimeError):
    """A token file exists but none of the reachable keys can decrypt it."""


_kc_note_printed = False


def _keychain_key() -> tuple[bytes | None, bool]:
    """Return (key or None, keychain_usable). Unusable = locked/headless session."""
    global _kc_note_printed
    try:
        import keyring

        existing = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
        return (existing.encode() if existing else None), True
    except Exception as e:
        if not _kc_note_printed:
            _kc_note_printed = True
            print(
                f"[gmail-mcp-private] NOTE: macOS Keychain unavailable in this session "
                f"({type(e).__name__}); using the key file if present.",
                file=sys.stderr,
            )
        return None, False


def _create_key(keychain_usable: bool) -> bytes:
    """Mint the very first key: Keychain preferred (key not on disk next to the
    ciphertext), else a key file created atomically at 0600 — loudly, so a later
    decrypt problem is diagnosable."""
    key = Fernet.generate_key()
    if keychain_usable:
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, KEYRING_KEY_NAME, key.decode())
            return key
        except Exception:
            pass  # fall through to the file
    _ensure_dirs()
    print(
        f"[gmail-mcp-private] WARNING: storing the encryption key on disk at {KEY_FILE} "
        f"(0600) because the macOS Keychain is unavailable. Keep FileVault on.",
        file=sys.stderr,
    )
    try:
        fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # raced another process; its key wins
        return KEY_FILE.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _fernet() -> MultiFernet:
    """Encrypt with the primary key; decrypt with EVERY key we can reach (Keychain first,
    then key file). A session where one source is unreachable can then never strand tokens
    written by another session under the other key, and once both sources are reachable
    again, refreshed tokens re-save under the Keychain key (self-healing)."""
    kc_key, kc_usable = _keychain_key()
    file_key = KEY_FILE.read_bytes() if KEY_FILE.exists() else None
    keys = [k for k in (kc_key, file_key) if k]
    if not keys:
        keys = [_create_key(kc_usable)]
    return MultiFernet([Fernet(k) for k in keys])


def _token_path(alias: str) -> pathlib.Path:
    return TOKENS_DIR / f"{validate_alias(alias)}.enc"


def load_oauth_client() -> dict:
    if not OAUTH_CLIENT_FILE.exists():
        raise FileNotFoundError(
            f"OAuth client file not found at {OAUTH_CLIENT_FILE}. "
            "Download your Desktop OAuth client JSON from Google Cloud Console and save it there."
        )
    harden_file(OAUTH_CLIENT_FILE)
    return json.loads(OAUTH_CLIENT_FILE.read_text())


def save_credentials(alias: str, creds: Credentials) -> None:
    _ensure_dirs()
    enc = _fernet().encrypt(creds.to_json().encode())
    dest = _token_path(alias)
    fd, tmp = tempfile.mkstemp(dir=str(TOKENS_DIR), prefix=".tmp-", suffix=".enc")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(enc)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_credentials(alias: str, scopes: list[str]) -> Credentials | None:
    p = _token_path(alias)
    if not p.exists():
        return None
    try:
        data = _fernet().decrypt(p.read_bytes())
    except InvalidToken:
        raise TokenDecryptError(
            f"Token for '{alias}' exists but can't be decrypted with the reachable key(s). "
            f"If this is an SSH/pre-login session the macOS Keychain may be locked — retry "
            f"from a logged-in GUI session. Otherwise re-run: python authenticate.py {alias}"
        )
    return Credentials.from_authorized_user_info(json.loads(data.decode()), scopes)


def get_credentials(alias: str, scopes: list[str]) -> Credentials:
    """Return valid credentials for an already-authorized account, refreshing if needed.
    Never launches an interactive flow."""
    creds = _load_credentials(alias, scopes)
    if creds is None:
        raise RuntimeError(
            f"Account '{alias}' is not authorized yet. Run:  python authenticate.py {alias}"
        )
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            raise RuntimeError(
                f"Authorization for '{alias}' was revoked or expired. "
                f"Re-run:  python authenticate.py {alias}"
            )
        save_credentials(alias, creds)
        return creds
    raise RuntimeError(
        f"Credentials for '{alias}' are invalid and cannot be refreshed. "
        f"Re-run:  python authenticate.py {alias}"
    )


def account_status(alias: str, scopes: list[str]) -> str:
    """Non-mutating status for an account — does NOT refresh or re-save tokens."""
    try:
        creds = _load_credentials(alias, scopes)
    except Exception as e:
        return f"unreadable ({type(e).__name__})"
    if creds is None:
        return "not authorized"
    if creds.valid:
        return "authorized"
    if creds.expired and creds.refresh_token:
        return "authorized (refreshes on use)"
    return "needs re-auth"


def authorize_interactive(alias: str, scopes: list[str]) -> Credentials:
    """Run the loopback OAuth flow in the browser. Called ONLY by authenticate.py."""
    validate_alias(alias)
    flow = InstalledAppFlow.from_client_config(load_oauth_client(), scopes)
    return flow.run_local_server(port=0, prompt="consent")

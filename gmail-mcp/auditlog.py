"""Append-only audit log for the Gmail MCP server.

Writes one JSON line per tool invocation to ~/.gmail-mcp-private/audit.log (0600, rotating).
Logs NON-CONTENT metadata only — tool name, account alias, ids, counts, lengths — never
email bodies, subjects, recipients, or query text. Logging must never break a tool, so all
errors are swallowed. The handler forces 0600 on the base file AND every rotated file.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from logging.handlers import RotatingFileHandler

CONFIG_DIR = pathlib.Path(os.path.expanduser("~/.gmail-mcp-private"))
LOG_FILE = CONFIG_DIR / "audit.log"

_logger: logging.Logger | None = None


class _Mode600RotatingHandler(RotatingFileHandler):
    """RotatingFileHandler that creates the base file — and every file produced on rollover —
    with 0600 perms (the stock handler re-creates the base file at the default umask)."""

    def _open(self):
        old = os.umask(0o077)
        try:
            stream = super()._open()
        finally:
            os.umask(old)
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    lg = logging.getLogger("gmail-mcp-private.audit")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    if not lg.handlers:
        handler = _Mode600RotatingHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(handler)
    _logger = lg
    return lg


def log(event: str, account: str = "", **meta) -> None:
    """Record a non-content event line. Never raises."""
    try:
        rec = {"ts": round(time.time(), 3), "event": event, "account": account}
        rec.update(meta)
        _get_logger().info(json.dumps(rec, ensure_ascii=False))
    except Exception:
        pass

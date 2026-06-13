"""Thin wrapper over the Gmail API: search, read, labels, archive/mark-read, drafts.

Uses only the official google-api-python-client. The only network calls this module makes
are to https://www.googleapis.com (Gmail). No telemetry, no other endpoints.
"""
from __future__ import annotations

import base64
import html
import re
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials


def service(creds: Credentials):
    # cache_discovery=False avoids writing a discovery cache file to disk.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_profile_email(creds: Credentials) -> str:
    return service(creds).users().getProfile(userId="me").execute().get("emailAddress", "")


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _b64d(data: str) -> bytes:
    """Decode base64url, tolerating missing padding (Gmail sometimes omits it)."""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _no_crlf(value: str | None, field: str) -> str | None:
    """Reject CR/LF in a user-supplied header value (blocks header injection regardless
    of interpreter version)."""
    if value is None:
        return value
    if "\r" in value or "\n" in value:
        raise ValueError(f"Illegal newline in '{field}'.")
    return value


def _strip_html(html_text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(payload: dict) -> str:
    """Recursively pull text/plain from a MIME payload; fall back to stripped text/html."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")
    if mime == "text/plain" and data:
        return _b64d(data).decode("utf-8", errors="replace")
    parts = payload.get("parts")
    if parts:
        for part in parts:  # prefer a top-level text/plain part
            if part.get("mimeType") == "text/plain":
                txt = _extract_body(part)
                if txt:
                    return txt
        chunks = [_extract_body(part) for part in parts]  # else recurse (multipart/*)
        joined = "\n".join(c for c in chunks if c)
        if joined:
            return joined
    if mime == "text/html" and data:
        return _strip_html(_b64d(data).decode("utf-8", errors="replace"))
    return ""


def search(creds, query: str, max_results: int = 10) -> list[dict]:
    svc = service(creds)
    resp = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []):
        full = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        h = full.get("payload", {}).get("headers", [])
        out.append(
            {
                "id": full["id"],
                "threadId": full.get("threadId", ""),
                "from": _header(h, "From"),
                "subject": _header(h, "Subject"),
                "date": _header(h, "Date"),
                "snippet": full.get("snippet", ""),
                "labelIds": full.get("labelIds", []),
            }
        )
    return out


def read_message(creds, message_id: str) -> dict:
    full = service(creds).users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = full.get("payload", {})
    h = payload.get("headers", [])
    return {
        "id": full.get("id", message_id),
        "threadId": full.get("threadId", ""),
        "from": _header(h, "From"),
        "to": _header(h, "To"),
        "cc": _header(h, "Cc"),
        "subject": _header(h, "Subject"),
        "date": _header(h, "Date"),
        "labelIds": full.get("labelIds", []),
        "body": _extract_body(payload),
    }


def read_thread(creds, thread_id: str) -> list[dict]:
    thread = service(creds).users().threads().get(userId="me", id=thread_id, format="full").execute()
    msgs = []
    for full in thread.get("messages", []):
        payload = full.get("payload", {})
        h = payload.get("headers", [])
        msgs.append(
            {
                "id": full.get("id", ""),
                "from": _header(h, "From"),
                "to": _header(h, "To"),
                "cc": _header(h, "Cc"),
                "subject": _header(h, "Subject"),
                "date": _header(h, "Date"),
                "labelIds": full.get("labelIds", []),
                "body": _extract_body(payload),
            }
        )
    return msgs


def list_labels(creds) -> list[dict]:
    resp = service(creds).users().labels().list(userId="me").execute()
    return [{"id": l["id"], "name": l["name"]} for l in resp.get("labels", [])]


# Adding these moves mail toward deletion (Trash auto-purges permanently after ~30 days),
# which would break this server's no-delete guarantee. Removing them (untrash) is fine.
_BLOCKED_ADD_LABELS = frozenset({"TRASH", "SPAM"})


def resolve_labels(creds, add: list[str], remove: list[str]) -> tuple[list[str], list[str]]:
    """Map NAMES or IDs to label IDs for both lists in a SINGLE labels.list call.
    System labels (INBOX/UNREAD/...) resolve via their name; unknown labels raise.
    (Gmail reserves system-label names, so a name can't ambiguously mean a user label.)
    ADDING TRASH or SPAM is refused — see _BLOCKED_ADD_LABELS."""
    labels = list_labels(creds)
    by_name = {l["name"].lower(): l["id"] for l in labels}
    ids = {l["id"] for l in labels}

    def resolve(tokens, blocked=frozenset()):
        out, unknown = [], []
        for t in tokens or []:
            lid = t if t in ids else by_name.get(t.lower())
            if lid is None:
                unknown.append(t)
                continue
            # check both the resolved id and the raw token, so the block holds even if
            # Gmail ever omits these system labels from labels.list
            if lid.upper() in blocked or t.strip().upper() in blocked:
                raise ValueError(
                    f"Refusing to add label '{t}': moving mail to Trash/Spam is blocked "
                    f"(Trash auto-purges after ~30 days; this server never deletes mail)."
                )
            out.append(lid)
        if unknown:
            raise ValueError(
                f"Unknown label(s): {', '.join(unknown)}. Use list_labels to see valid names/ids."
            )
        return out

    return resolve(add, blocked=_BLOCKED_ADD_LABELS), resolve(remove)


def modify_labels(creds, message_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    body = {"addLabelIds": add or [], "removeLabelIds": remove or []}
    return service(creds).users().messages().modify(userId="me", id=message_id, body=body).execute()


def create_draft(
    creds,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    reply_to_message_id: str | None = None,
    thread_id: str | None = None,
) -> tuple[dict, str]:
    """Create a draft. Returns (api_result, subject_actually_used) so callers can audit the
    real subject (which is overridden to match the thread on replies)."""
    svc = service(creds)
    _no_crlf(to, "to")
    _no_crlf(cc, "cc")
    msg = MIMEText(body)  # Python 3 MIMEText auto-uses utf-8 when the body isn't ascii
    if reply_to_message_id:
        orig = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=reply_to_message_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"],
            )
            .execute()
        )
        oh = orig.get("payload", {}).get("headers", [])
        orig_subject = _header(oh, "Subject")
        if orig_subject:  # force subject to match thread (sanitized; derived from existing mail)
            base = re.sub(r"[\r\n]+", " ", re.sub(r"^(re:\s*)+", "", orig_subject, flags=re.I)).strip()
            subject = f"Re: {base}"
        else:
            _no_crlf(subject, "subject")
        mid = _header(oh, "Message-ID")
        if mid:
            msg["In-Reply-To"] = mid
            msg["References"] = (_header(oh, "References") + " " + mid).strip()
        thread_id = thread_id or orig.get("threadId")
    else:
        _no_crlf(subject, "subject")
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = {"message": {"raw": raw}}
    if thread_id:
        draft["message"]["threadId"] = thread_id
    result = svc.users().drafts().create(userId="me", body=draft).execute()
    return result, subject


def send_message(creds, to: str, subject: str, body: str, cc: str | None = None) -> dict:
    """Only invoked when enable_send=true. Defined here but never reached otherwise."""
    _no_crlf(to, "to")
    _no_crlf(cc, "cc")
    _no_crlf(subject, "subject")
    msg = MIMEText(body)
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service(creds).users().messages().send(userId="me", body={"raw": raw}).execute()

#!/usr/bin/env python3
"""Minimal native Gmail REST tools.

Read-only Gmail REST calls using OSIA's local ``gmail-rest`` OAuth blob.
Phase 1 registers the safe read-only subset in ``osia.agent``.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from typing import Any
from urllib.parse import quote

from osia.integrations.google_oauth import (
    GoogleOAuthError,
    authorize,
    authorized_request,
)


GMAIL_REST_KEY = "gmail-rest"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

BODY_CHAR_LIMIT = 4000
HEADER_CHAR_LIMIT = 1000
SNIPPET_CHAR_LIMIT = 1000
MAX_LATEST_RESULTS = 25
MAX_ATTACHMENTS = 50
MAX_THREAD_MESSAGES = 10

NOT_CONNECTED_ERROR = (
    "ERROR: Gmail is not connected. Open Settings > Gmail and connect the "
    "native Gmail integration. The legacy Google MCP Gmail connector is "
    "temporarily unavailable."
)
RECONNECT_ERROR = (
    "ERROR: Gmail connection needs to be refreshed. Open Settings > Gmail and "
    "reconnect the native Gmail integration. The legacy Google MCP Gmail "
    "connector is temporarily unavailable."
)
PERMISSION_ERROR = (
    "ERROR: Gmail API permission error. Reconnect native Gmail with the Gmail "
    "read-only scope. The legacy Google MCP Gmail connector is temporarily "
    "unavailable."
)
NETWORK_ERROR = (
    "ERROR: Gmail API request failed because the network or Google API was "
    "unavailable. Try again in a moment."
)


class GmailRestError(RuntimeError):
    """Token-safe Gmail REST failure."""


def gmail_connect() -> dict:
    """Run the interactive Gmail REST OAuth flow and return a safe summary."""
    blob = authorize(GMAIL_REST_KEY, [GMAIL_READONLY_SCOPE], force_consent=True)
    token = blob.get("token") if isinstance(blob, dict) else {}
    scope = token.get("scope") if isinstance(token, dict) else ""
    return {
        "integration": GMAIL_REST_KEY,
        "connected": True,
        "scope": scope or GMAIL_READONLY_SCOPE,
        "expires_at": blob.get("expires_at") if isinstance(blob, dict) else None,
    }


def gmail_profile(*, interactive: bool = False) -> dict | str:
    """Return the Gmail profile for the connected account."""
    def run() -> dict:
        data = _gmail_get("/profile", interactive=interactive)
        return {
            "emailAddress": data.get("emailAddress"),
            "messagesTotal": data.get("messagesTotal"),
            "threadsTotal": data.get("threadsTotal"),
            "historyId": data.get("historyId"),
        }

    return _safe_gmail_result(run)


def gmail_list_labels(*, interactive: bool = False) -> dict | str:
    """Return Gmail labels with basic count metadata."""
    def run() -> dict:
        data = _gmail_get("/labels", interactive=interactive)
        labels = data.get("labels") if isinstance(data, dict) else []
        clean = []
        for label in labels if isinstance(labels, list) else []:
            if not isinstance(label, dict):
                continue
            clean.append({
                "id": label.get("id"),
                "name": label.get("name"),
                "type": label.get("type"),
                "messageListVisibility": label.get("messageListVisibility"),
                "labelListVisibility": label.get("labelListVisibility"),
                "messagesTotal": label.get("messagesTotal"),
                "messagesUnread": label.get("messagesUnread"),
                "threadsTotal": label.get("threadsTotal"),
                "threadsUnread": label.get("threadsUnread"),
            })
        return {"labels": clean}

    return _safe_gmail_result(run)


def gmail_list_latest(
    max_results: int = 5, *, interactive: bool = False
) -> dict | str:
    """List recent message summaries and fetch each message's metadata."""
    def run() -> dict | str:
        limit = _coerce_limit(max_results)
        data = _gmail_get(
            "/messages",
            params={"maxResults": limit},
            interactive=interactive,
        )
        raw_messages = data.get("messages") if isinstance(data, dict) else []
        summaries = []
        for item in raw_messages if isinstance(raw_messages, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            summary = gmail_get_message_metadata(str(item["id"]),
                                                 interactive=interactive)
            if isinstance(summary, str) and summary.startswith("ERROR:"):
                return summary
            summaries.append(summary)
        return {
            "messages": summaries,
            "nextPageToken": data.get("nextPageToken"),
            "resultSizeEstimate": data.get("resultSizeEstimate"),
        }

    return _safe_gmail_result(run)


def gmail_search(
    query: str, max_results: int = 10, *, interactive: bool = False
) -> dict | str:
    """Search Gmail with a Gmail query string and return message summaries."""
    def run() -> dict | str:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise GmailRestError("query is required.")
        limit = _coerce_limit(max_results)
        data = _gmail_get(
            "/messages",
            params={"q": clean_query, "maxResults": limit},
            interactive=interactive,
        )
        raw_messages = data.get("messages") if isinstance(data, dict) else []
        summaries = []
        for item in raw_messages if isinstance(raw_messages, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            summary = gmail_get_message_metadata(str(item["id"]),
                                                 interactive=interactive)
            if isinstance(summary, str) and summary.startswith("ERROR:"):
                return summary
            summaries.append(summary)
        return {
            "query": clean_query,
            "messages": summaries,
            "nextPageToken": data.get("nextPageToken"),
            "resultSizeEstimate": data.get("resultSizeEstimate"),
        }

    return _safe_gmail_result(run)


def gmail_get_message(
    message_id: str, *, interactive: bool = False
) -> dict | str:
    """Return a bounded, read-only full Gmail message representation."""
    def run() -> dict:
        clean_id = str(message_id or "").strip()
        if not clean_id:
            raise GmailRestError("message_id is required.")
        data = _gmail_get(
            f"/messages/{quote(clean_id, safe='')}",
            params={"format": "full"},
            interactive=interactive,
        )
        return _extract_message(data, include_body=True)

    return _safe_gmail_result(run)


def gmail_read_message(
    message_id: str, *, interactive: bool = False
) -> dict | str:
    """Agent-facing alias for reading one bounded Gmail message."""
    return gmail_get_message(message_id, interactive=interactive)


def gmail_read_thread(
    thread_id: str, *, interactive: bool = False
) -> dict | str:
    """Return a bounded, read-only Gmail thread representation."""
    def run() -> dict:
        clean_id = str(thread_id or "").strip()
        if not clean_id:
            raise GmailRestError("thread_id is required.")
        data = _gmail_get(
            f"/threads/{quote(clean_id, safe='')}",
            params={"format": "full"},
            interactive=interactive,
        )
        raw_messages = data.get("messages") if isinstance(data, dict) else []
        raw_messages = raw_messages if isinstance(raw_messages, list) else []
        messages = [
            _extract_message(message, include_body=True)
            for message in raw_messages[:MAX_THREAD_MESSAGES]
            if isinstance(message, dict)
        ]
        return {
            "id": data.get("id") or clean_id,
            "threadId": data.get("id") or clean_id,
            "historyId": data.get("historyId"),
            "messageCount": len(raw_messages),
            "messagesTruncated": len(raw_messages) > len(messages),
            "messages": messages,
        }

    return _safe_gmail_result(run)


def gmail_get_message_metadata(
    message_id: str, *, interactive: bool = False
) -> dict | str:
    """Return a bounded metadata summary for one Gmail message."""
    def run() -> dict:
        clean_id = str(message_id or "").strip()
        if not clean_id:
            raise GmailRestError("message_id is required.")
        params = [
            ("format", "metadata"),
            ("metadataHeaders", "Subject"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "To"),
            ("metadataHeaders", "Date"),
        ]
        data = _gmail_get(
            f"/messages/{quote(clean_id, safe='')}",
            params=params,
            interactive=interactive,
        )
        return _extract_message(data, include_body=False)

    return _safe_gmail_result(run)


def _safe_gmail_result(func):
    try:
        return func()
    except (GoogleOAuthError, GmailRestError) as exc:
        return _safe_gmail_error(exc)


def _safe_gmail_error(exc: Exception) -> str:
    text = str(exc or "").strip()
    if text.startswith("ERROR:"):
        return text
    lower = text.lower()
    if (
        "oauth token missing" in lower
        or "no refresh token stored" in lower
        or "missing required scopes" in lower
    ):
        return NOT_CONNECTED_ERROR
    if (
        "401" in lower
        or "invalid_grant" in lower
        or "refresh failed" in lower
        or "expired" in lower
    ):
        return RECONNECT_ERROR
    if (
        "403" in lower
        or "permission" in lower
        or "insufficient" in lower
        or "forbidden" in lower
    ):
        return PERMISSION_ERROR
    if (
        "request failed" in lower
        or "timeout" in lower
        or "connection" in lower
        or "network" in lower
    ):
        return NETWORK_ERROR
    prefix = "Gmail request failed"
    if isinstance(exc, GoogleOAuthError):
        prefix = "Gmail OAuth failed"
    detail = _sanitize_error_detail(text)
    return f"ERROR: {prefix}: {detail}" if detail else f"ERROR: {prefix}."


def _sanitize_error_detail(text: str) -> str:
    detail = str(text or "")
    detail = re.sub(
        r"(?i)(access_token|refresh_token|client_secret)\s*[:=]\s*[^,\s}]+",
        r"\1=<redacted>",
        detail,
    )
    detail = re.sub(r"\s+", " ", detail).strip()
    return detail[:300]


def _gmail_get(
    path: str,
    *,
    params: dict | list[tuple[str, Any]] | None = None,
    interactive: bool = True,
) -> dict:
    url = GMAIL_API_BASE + path
    response = authorized_request(
        GMAIL_REST_KEY,
        "GET",
        url,
        [GMAIL_READONLY_SCOPE],
        interactive=interactive,
        params=params,
    )
    if response.status_code >= 400:
        raise GmailRestError("Gmail REST error: " + _gmail_error_summary(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise GmailRestError("Gmail REST response was not JSON.") from exc
    if not isinstance(data, dict):
        raise GmailRestError("Gmail REST response had an invalid shape.")
    return data


def _extract_message(data: dict, *, include_body: bool) -> dict:
    payload = data.get("payload") if isinstance(data, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    headers = _extract_headers(payload)
    result = {
        "id": data.get("id"),
        "threadId": data.get("threadId"),
        "labelIds": data.get("labelIds") if isinstance(data.get("labelIds"), list) else [],
        "internalDate": data.get("internalDate"),
        "historyId": data.get("historyId"),
        "snippet": _bounded_str(data.get("snippet"), SNIPPET_CHAR_LIMIT),
        "headers": headers,
    }
    attachments = _extract_attachments(payload)
    if attachments:
        result["attachments"] = attachments
    if include_body:
        body_text, mime_type = _extract_body_text(payload)
        result["bodyMimeType"] = mime_type
        result["body"], result["bodyTruncated"] = _bounded_text(
            body_text, BODY_CHAR_LIMIT
        )
    return result


def _extract_headers(payload: dict) -> dict:
    wanted = {
        "subject": "subject",
        "from": "from",
        "to": "to",
        "date": "date",
    }
    output = {value: "" for value in wanted.values()}
    for header in payload.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        key = wanted.get(name)
        if key:
            output[key] = _bounded_str(header.get("value"), HEADER_CHAR_LIMIT)
    return output


def _extract_body_text(payload: dict) -> tuple[str, str | None]:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in _walk_parts(payload):
        mime_type = str(part.get("mimeType") or "").split(";", 1)[0].lower()
        data = ((part.get("body") or {}).get("data")
                if isinstance(part.get("body"), dict) else None)
        if not isinstance(data, str) or not data:
            continue
        decoded = _decode_gmail_body(data)
        if not decoded:
            continue
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(_html_to_text(decoded))
    if plain_parts:
        return _normalize_text("\n\n".join(plain_parts)), "text/plain"
    if html_parts:
        return _normalize_text("\n\n".join(html_parts)), "text/html"
    return "", None


def _extract_attachments(payload: dict) -> list[dict]:
    attachments = []
    for part in _walk_parts(payload):
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        filename = str(part.get("filename") or "").strip()
        attachment_id = body.get("attachmentId") if isinstance(body, dict) else None
        if not filename and not attachment_id:
            continue
        attachments.append({
            "filename": _bounded_str(filename, 300),
            "mimeType": part.get("mimeType"),
            "size": body.get("size") if isinstance(body, dict) else None,
            "attachmentId": attachment_id,
        })
        if len(attachments) >= MAX_ATTACHMENTS:
            break
    return attachments


def _walk_parts(part: dict):
    if not isinstance(part, dict):
        return
    yield part
    children = part.get("parts")
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, dict):
            yield from _walk_parts(child)


def _decode_gmail_body(data: str) -> str:
    try:
        padded = data + ("=" * (-len(data) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def _normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.split("\n")]
    return "\n".join(line for line in lines if line)


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n[truncated]", True


def _bounded_str(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _coerce_limit(max_results: int) -> int:
    try:
        value = int(max_results)
    except Exception:
        value = 5
    return max(1, min(value, MAX_LATEST_RESULTS))


def _gmail_error_summary(response: Any) -> str:
    try:
        data = response.json()
    except ValueError:
        body = response.text[:500]
        return f"HTTP {response.status_code}: {body}"
    if not isinstance(data, dict):
        return f"HTTP {response.status_code}"
    error = data.get("error")
    if isinstance(error, dict):
        status = str(error.get("status") or "").strip()
        message = str(error.get("message") or "").strip()
        detail = ": ".join(part for part in (status, message) if part)
        return f"HTTP {response.status_code}: {detail[:500]}" if detail else (
            f"HTTP {response.status_code}"
        )
    return f"HTTP {response.status_code}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="OSIA Gmail REST proof-of-life CLI."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--connect", action="store_true",
                       help="run the Desktop OAuth loopback flow")
    group.add_argument("--profile", action="store_true",
                       help="fetch the Gmail profile")
    group.add_argument("--labels", action="store_true",
                       help="list Gmail labels")
    group.add_argument("--latest", type=int, metavar="N",
                       help="list the latest N message summaries")
    group.add_argument("--search", metavar="QUERY",
                       help="search Gmail with a Gmail query")
    group.add_argument("--message", metavar="ID",
                       help="fetch one full message by id")
    group.add_argument("--thread", metavar="ID",
                       help="fetch one full thread by id")
    parser.add_argument("--max-results", type=int, default=10,
                        help="maximum search results for --search")
    parser.add_argument("--no-browser", action="store_true",
                        help="fail instead of starting OAuth when no token works")
    args = parser.parse_args(argv)

    interactive = not args.no_browser
    try:
        if args.connect:
            if args.no_browser:
                raise GmailRestError("--connect requires browser OAuth.")
            result = gmail_connect()
        elif args.profile:
            result = gmail_profile(interactive=interactive)
        elif args.labels:
            result = gmail_list_labels(interactive=interactive)
        elif args.latest is not None:
            result = gmail_list_latest(args.latest, interactive=interactive)
        elif args.search is not None:
            result = gmail_search(args.search, args.max_results,
                                  interactive=interactive)
        elif args.message is not None:
            result = gmail_get_message(args.message, interactive=interactive)
        else:
            result = gmail_read_thread(args.thread, interactive=interactive)
    except (GoogleOAuthError, GmailRestError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if isinstance(result, str) and result.startswith("ERROR:"):
        print(result)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

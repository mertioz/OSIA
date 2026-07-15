#!/usr/bin/env python3
"""Minimal native Gmail REST tools.

Read-only Gmail REST calls using OSIA's local ``gmail-rest`` OAuth blob.
Phase 1 registers the safe read-only subset in ``osia.agent``.
"""

from __future__ import annotations

import argparse
import base64
from email.message import EmailMessage
from email.utils import getaddresses
import html
import json
import re
from typing import Any
from urllib.parse import quote

from osia.integrations.google_oauth import (
    GoogleOAuthError,
    authorize,
    authorized_request,
    ensure_access_token,
    revoke_token,
)
from osia.config import delete_oauth_token, get_oauth_token


GMAIL_REST_KEY = "gmail-rest"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_COMPOSE_SCOPES = (GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE)
GMAIL_MODIFY_SCOPES = (
    GMAIL_READONLY_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_MODIFY_SCOPE,
)
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

BODY_CHAR_LIMIT = 4000
HEADER_CHAR_LIMIT = 1000
SNIPPET_CHAR_LIMIT = 1000
MAX_LATEST_RESULTS = 25
MAX_ATTACHMENTS = 50
MAX_THREAD_MESSAGES = 10
DRAFT_SUBJECT_CHAR_LIMIT = 300
DRAFT_BODY_CHAR_LIMIT = 20000
DRAFT_RECIPIENT_LIMIT = 50
DRAFT_PREVIEW_CHAR_LIMIT = 240
CONFIRMATION_PREVIEW_CHAR_LIMIT = 240

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_GMAIL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

NOT_CONNECTED_ERROR = (
    "ERROR: Gmail is not connected. Open Settings > Gmail and connect the "
    "native Gmail integration."
)
RECONNECT_ERROR = (
    "ERROR: Gmail connection needs to be refreshed. Open Settings > Gmail and "
    "reconnect the native Gmail integration."
)
PERMISSION_ERROR = (
    "ERROR: Gmail API permission error. Reconnect native Gmail with the Gmail "
    "read-only scope."
)
NETWORK_ERROR = (
    "ERROR: Gmail API request failed because the network or Google API was "
    "unavailable. Try again in a moment."
)
COMPOSE_UPGRADE_ERROR = (
    "ERROR: Gmail compose permission required. Open Settings > Gmail and use "
    "Elevate OSIA permissions from the Gmail connector card. No Gmail data "
    "was changed."
)
MODIFY_UPGRADE_ERROR = (
    "ERROR: Gmail modify permission required. Open Settings > Gmail and use "
    "Elevate OSIA permissions from the Gmail connector card. No Gmail data "
    "was changed."
)
CONFIRMATION_REQUIRED_ERROR = (
    "ERROR: Gmail write action requires approval, but no confirmation UI is "
    "available and unattended writes are disabled. No Gmail data was changed."
)
WRITE_CONFIRMATION_REQUIRED_ERROR = (
    "ERROR: Gmail write action requires approval, but no confirmation UI is "
    "available and unattended writes are disabled. No Gmail data was changed."
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
        **_gmail_capability_payload(token, connected=True),
    }


def gmail_upgrade_compose_permissions() -> dict:
    """Run explicit re-consent for Gmail read-only + compose scopes."""
    blob = authorize(GMAIL_REST_KEY, GMAIL_COMPOSE_SCOPES, force_consent=True)
    token = blob.get("token") if isinstance(blob, dict) else {}
    token = token if isinstance(token, dict) else {}
    return {
        "integration": GMAIL_REST_KEY,
        "connected": True,
        "scope": token.get("scope") or " ".join(GMAIL_COMPOSE_SCOPES),
        "expires_at": blob.get("expires_at") if isinstance(blob, dict) else None,
        **_gmail_capability_payload(token, connected=True),
    }


def gmail_upgrade_modify_permissions() -> dict:
    """Run explicit re-consent for Gmail read-only + compose + modify scopes."""
    blob = authorize(GMAIL_REST_KEY, GMAIL_MODIFY_SCOPES, force_consent=True)
    token = blob.get("token") if isinstance(blob, dict) else {}
    token = token if isinstance(token, dict) else {}
    return {
        "integration": GMAIL_REST_KEY,
        "connected": True,
        "scope": token.get("scope") or " ".join(GMAIL_MODIFY_SCOPES),
        "expires_at": blob.get("expires_at") if isinstance(blob, dict) else None,
        **_gmail_capability_payload(token, connected=True),
    }


def gmail_disconnect() -> dict:
    """Revoke and delete only the native Gmail REST OAuth blob."""
    revoked = revoke_token(GMAIL_REST_KEY)
    delete_oauth_token(GMAIL_REST_KEY)
    return {
        "integration": GMAIL_REST_KEY,
        "connected": False,
        "status": "disconnected",
        "revoked": bool(revoked),
    }


def gmail_connection_status(*, validate: bool = True) -> dict:
    """Return token-safe native Gmail REST connection status for UI surfaces."""
    base = {
        "integration": GMAIL_REST_KEY,
        "connected": False,
        "status": "disconnected",
        "emailAddress": None,
        "scope": GMAIL_READONLY_SCOPE,
        "error": "",
        **_gmail_capability_payload({}, connected=False),
    }
    try:
        blob = get_oauth_token(GMAIL_REST_KEY)
    except Exception:
        return dict(base, status="failed",
                    error="Unable to read the local Gmail token store.")
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}
    if not token.get("access_token") and not token.get("refresh_token"):
        return base
    capability = _gmail_capability_payload(token, connected=True)
    if not _token_has_readonly_scope(token):
        return dict(
            base,
            status="failed",
            error=PERMISSION_ERROR.removeprefix("ERROR: ").strip(),
            scope=token.get("scope") or "",
            **capability,
        )
    if not validate:
        return dict(
            base,
            connected=True,
            status="connected",
            scope=token.get("scope") or GMAIL_READONLY_SCOPE,
            **capability,
        )

    profile = gmail_profile(interactive=False)
    if isinstance(profile, dict):
        return {
            "integration": GMAIL_REST_KEY,
            "connected": True,
            "status": "connected",
            "emailAddress": profile.get("emailAddress"),
            "messagesTotal": profile.get("messagesTotal"),
            "threadsTotal": profile.get("threadsTotal"),
            "scope": token.get("scope") or GMAIL_READONLY_SCOPE,
            "error": "",
            **capability,
        }
    error = str(profile or "").strip()
    if error == NOT_CONNECTED_ERROR:
        status = "disconnected"
    else:
        status = "failed"
    return dict(
        base,
        status=status,
        error=error.removeprefix("ERROR: ").strip(),
        scope=token.get("scope") or GMAIL_READONLY_SCOPE,
        **capability,
    )


def gmail_compose_capability_status(*, validate: bool = False) -> dict:
    """Return safe Gmail capability status for read-only/compose/modify checks."""
    return gmail_connection_status(validate=validate)


def gmail_modify_capability_status(*, validate: bool = False) -> dict:
    """Return safe Gmail capability status for mailbox-action UI checks."""
    return gmail_connection_status(validate=validate)


def gmail_reconnect_scopes() -> tuple[str, ...]:
    """Return the least scope set that preserves current Gmail capabilities."""
    try:
        blob = get_oauth_token(GMAIL_REST_KEY)
    except Exception:
        blob = {}
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}
    if _token_has_modify_scope(token):
        return GMAIL_MODIFY_SCOPES
    if _token_has_compose_scope(token):
        return GMAIL_COMPOSE_SCOPES
    return (GMAIL_READONLY_SCOPE,)


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


def gmail_create_draft(
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    *,
    interactive: bool = False,
) -> dict | str:
    """Create a plain-text Gmail draft through connector write safety."""
    def run() -> dict:
        draft = _prepare_plain_text_draft(to, subject, body, cc=cc, bcc=bcc)
        _require_compose_permission()
        confirmed = _confirm_gmail_draft_creation(draft)
        if not confirmed:
            return {
                "status": "cancelled",
                "changed": False,
                "reason": "user_declined_confirmation",
                "to": draft["to"],
                "cc": draft["cc"],
                "bccCount": len(draft["bcc"]),
                "subject": draft["subject"],
                "bodyChars": len(draft["body"]),
            }
        data = _gmail_post(
            "/drafts",
            json_body={"message": {"raw": draft["raw"]}},
            scopes=GMAIL_COMPOSE_SCOPES,
            interactive=interactive,
        )
        message = data.get("message") if isinstance(data, dict) else {}
        message = message if isinstance(message, dict) else {}
        return {
            "status": "draft_created",
            "changed": True,
            "draftId": data.get("id"),
            "messageId": message.get("id"),
            "threadId": message.get("threadId"),
            "to": draft["to"],
            "cc": draft["cc"],
            "bccCount": len(draft["bcc"]),
            "recipientCount": draft["recipientCount"],
            "subject": draft["subject"],
            "bodyChars": len(draft["body"]),
        }

    return _safe_gmail_result(run)


def gmail_send_draft(
    draft_id: str,
    *,
    interactive: bool = False,
) -> dict | str:
    """Send an existing Gmail draft through connector write safety."""
    def run() -> dict:
        clean_id = _clean_gmail_id(draft_id, "draft_id")
        _require_compose_permission()
        confirmed = _confirm_gmail_write(
            "gmail_send_draft",
            _send_draft_confirmation_summary({"draftId": clean_id}),
        )
        if not confirmed:
            return {
                "status": "cancelled",
                "changed": False,
                "reason": "user_declined_confirmation",
                "draftId": clean_id,
                "to": "",
                "cc": "",
                "bccPresent": False,
                "subject": "",
            }
        draft = _fetch_draft_summary(clean_id, interactive=interactive)
        data = _gmail_post_no_retry(
            "/drafts/send",
            json_body={"id": clean_id},
            scopes=GMAIL_COMPOSE_SCOPES,
            interactive=interactive,
        )
        return {
            "status": "draft_sent",
            "changed": True,
            "draftId": clean_id,
            "messageId": data.get("id"),
            "threadId": data.get("threadId"),
            "to": draft.get("to") or "",
            "cc": draft.get("cc") or "",
            "bccPresent": bool(draft.get("bcc")),
            "subject": draft.get("subject") or "",
        }

    return _safe_gmail_result(run)


def gmail_mark_read(
    message_id: str,
    *,
    interactive: bool = False,
) -> dict | str:
    """Mark one Gmail message as read through connector write safety."""
    return _gmail_confirmed_message_modify(
        message_id,
        action="mark_read",
        tool_name="gmail_mark_read",
        summary_action="Mark Gmail message read",
        add_labels=(),
        remove_labels=("UNREAD",),
        interactive=interactive,
    )


def gmail_mark_unread(
    message_id: str,
    *,
    interactive: bool = False,
) -> dict | str:
    """Mark one Gmail message as unread through connector write safety."""
    return _gmail_confirmed_message_modify(
        message_id,
        action="mark_unread",
        tool_name="gmail_mark_unread",
        summary_action="Mark Gmail message unread",
        add_labels=("UNREAD",),
        remove_labels=(),
        interactive=interactive,
    )


def gmail_apply_label(
    message_id: str,
    label: str,
    *,
    interactive: bool = False,
) -> dict | str:
    """Apply one existing user Gmail label through connector write safety."""
    return _gmail_confirmed_label_modify(
        message_id,
        label,
        action="label_applied",
        tool_name="gmail_apply_label",
        summary_action="Apply Gmail label",
        add=True,
        interactive=interactive,
    )


def gmail_remove_label(
    message_id: str,
    label: str,
    *,
    interactive: bool = False,
) -> dict | str:
    """Remove one existing user Gmail label through connector write safety."""
    return _gmail_confirmed_label_modify(
        message_id,
        label,
        action="label_removed",
        tool_name="gmail_remove_label",
        summary_action="Remove Gmail label",
        add=False,
        interactive=interactive,
    )


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
    scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,),
) -> dict:
    url = GMAIL_API_BASE + path
    response = authorized_request(
        GMAIL_REST_KEY,
        "GET",
        url,
        scopes,
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


def _gmail_post_no_retry(
    path: str,
    *,
    json_body: dict,
    scopes: tuple[str, ...],
    interactive: bool = True,
) -> dict:
    """POST once to Gmail. Used for send to avoid duplicate-send retries."""
    url = GMAIL_API_BASE + path
    token = ensure_access_token(GMAIL_REST_KEY, scopes, interactive=interactive)
    try:
        import requests  # type: ignore
        response = requests.request(
            "POST",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=30.0,
            json=json_body,
        )
    except ImportError as exc:
        raise GoogleOAuthError(
            "Python package 'requests' is required for Google REST OAuth."
        ) from exc
    except requests.RequestException as exc:
        raise GoogleOAuthError(
            f"Authenticated REST request failed: {type(exc).__name__}"
        ) from exc
    if response.status_code >= 400:
        raise GmailRestError("Gmail REST error: " + _gmail_error_summary(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise GmailRestError("Gmail REST response was not JSON.") from exc
    if not isinstance(data, dict):
        raise GmailRestError("Gmail REST response had an invalid shape.")
    return data


def _gmail_post(
    path: str,
    *,
    json_body: dict,
    scopes: tuple[str, ...],
    interactive: bool = True,
) -> dict:
    url = GMAIL_API_BASE + path
    response = authorized_request(
        GMAIL_REST_KEY,
        "POST",
        url,
        scopes,
        interactive=interactive,
        json=json_body,
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


def _fetch_draft_summary(draft_id: str, *, interactive: bool) -> dict:
    params = [
        ("format", "metadata"),
        ("metadataHeaders", "To"),
        ("metadataHeaders", "Cc"),
        ("metadataHeaders", "Bcc"),
        ("metadataHeaders", "Subject"),
    ]
    data = _gmail_get(
        f"/drafts/{quote(draft_id, safe='')}",
        params=params,
        scopes=GMAIL_COMPOSE_SCOPES,
        interactive=interactive,
    )
    message = data.get("message") if isinstance(data, dict) else {}
    message = message if isinstance(message, dict) else {}
    return {
        "draftId": data.get("id") or draft_id,
        "messageId": message.get("id"),
        "threadId": message.get("threadId"),
        "to": _message_header_value(message, "To"),
        "cc": _message_header_value(message, "Cc"),
        "bcc": _message_header_value(message, "Bcc"),
        "subject": _message_header_value(message, "Subject"),
        "snippet": _bounded_str(message.get("snippet"), CONFIRMATION_PREVIEW_CHAR_LIMIT),
    }


def _gmail_confirmed_message_modify(
    message_id: str,
    *,
    action: str,
    tool_name: str,
    summary_action: str,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
    interactive: bool,
) -> dict | str:
    def run() -> dict:
        clean_id = _clean_gmail_id(message_id, "message_id")
        _require_modify_permission()
        confirmed = _confirm_gmail_write(
            tool_name,
            _message_modify_confirmation_summary(
                summary_action,
                clean_id,
                add_labels=add_labels,
                remove_labels=remove_labels,
            ),
        )
        if not confirmed:
            return {
                "status": "cancelled",
                "changed": False,
                "reason": "user_declined_confirmation",
                "messageId": clean_id,
                "action": action,
            }
        data = _modify_message_labels(
            clean_id,
            add_labels=add_labels,
            remove_labels=remove_labels,
            interactive=interactive,
        )
        return {
            "status": action,
            "changed": True,
            "messageId": data.get("id") or clean_id,
            "threadId": data.get("threadId"),
            "labelIds": data.get("labelIds") if isinstance(data.get("labelIds"), list) else [],
        }

    return _safe_gmail_result(run)


def _gmail_confirmed_label_modify(
    message_id: str,
    label: str,
    *,
    action: str,
    tool_name: str,
    summary_action: str,
    add: bool,
    interactive: bool,
) -> dict | str:
    def run() -> dict:
        clean_id = _clean_gmail_id(message_id, "message_id")
        clean_label = _clean_label_query(label)
        _require_modify_permission()
        confirmed = _confirm_gmail_write(
            tool_name,
            _label_modify_confirmation_summary(summary_action, clean_id, clean_label),
        )
        if not confirmed:
            return {
                "status": "cancelled",
                "changed": False,
                "reason": "user_declined_confirmation",
                "messageId": clean_id,
                "label": clean_label,
            }
        label_info = _resolve_user_label_exact(clean_label, interactive=interactive)
        data = _modify_message_labels(
            clean_id,
            add_labels=(label_info["id"],) if add else (),
            remove_labels=() if add else (label_info["id"],),
            interactive=interactive,
        )
        return {
            "status": action,
            "changed": True,
            "messageId": data.get("id") or clean_id,
            "threadId": data.get("threadId"),
            "label": label_info["name"],
            "labelId": label_info["id"],
            "labelIds": data.get("labelIds") if isinstance(data.get("labelIds"), list) else [],
        }

    return _safe_gmail_result(run)


def _modify_message_labels(
    message_id: str,
    *,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
    interactive: bool,
) -> dict:
    return _gmail_post(
        f"/messages/{quote(message_id, safe='')}/modify",
        json_body={
            "addLabelIds": list(add_labels),
            "removeLabelIds": list(remove_labels),
        },
        scopes=(GMAIL_MODIFY_SCOPE,),
        interactive=interactive,
    )


def _resolve_user_label_exact(label: str, *, interactive: bool) -> dict:
    data = _gmail_get(
        "/labels",
        scopes=(GMAIL_MODIFY_SCOPE,),
        interactive=interactive,
    )
    labels = data.get("labels") if isinstance(data, dict) else []
    matches: list[dict] = []
    for item in labels if isinstance(labels, list) else []:
        if not isinstance(item, dict):
            continue
        label_id = str(item.get("id") or "")
        name = str(item.get("name") or "")
        if label == label_id or label == name:
            matches.append(item)
    if not matches:
        raise GmailRestError(
            "label must exactly match an existing user label name or id."
        )
    if len(matches) > 1:
        raise GmailRestError("label matched more than one Gmail label.")
    match = matches[0]
    if str(match.get("type") or "").lower() != "user":
        raise GmailRestError(
            "only existing user labels can be applied or removed here."
        )
    label_id = str(match.get("id") or "").strip()
    name = str(match.get("name") or "").strip()
    if not label_id or not name:
        raise GmailRestError("Gmail label had an invalid shape.")
    return {"id": label_id, "name": name}


def _prepare_plain_text_draft(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None,
    bcc: str | list[str] | None,
) -> dict:
    to_list = _parse_recipients(to, "to", required=True)
    cc_list = _parse_recipients(cc, "cc", required=False)
    bcc_list = _parse_recipients(bcc, "bcc", required=False)
    total = len(to_list) + len(cc_list) + len(bcc_list)
    if total > DRAFT_RECIPIENT_LIMIT:
        raise GmailRestError(
            f"recipient count exceeds {DRAFT_RECIPIENT_LIMIT}."
        )
    clean_subject = _clean_subject(subject)
    clean_body = _clean_draft_body(body)
    message = EmailMessage()
    message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    if bcc_list:
        message["Bcc"] = ", ".join(bcc_list)
    message["Subject"] = clean_subject
    message.set_content(clean_body, subtype="plain", charset="utf-8")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    return {
        "raw": raw,
        "to": to_list,
        "cc": cc_list,
        "bcc": bcc_list,
        "recipientCount": total,
        "subject": clean_subject,
        "body": clean_body,
    }


def _parse_recipients(
    value: str | list[str] | None, field: str, *, required: bool
) -> list[str]:
    if value is None:
        raw_items: list[str] = []
    elif isinstance(value, (list, tuple)):
        raw_items = [str(item or "") for item in value]
    else:
        raw_items = [str(value or "")]
    raw_text = ", ".join(item.strip() for item in raw_items if item.strip())
    if not raw_text:
        if required:
            raise GmailRestError(f"{field} is required.")
        return []
    parsed = getaddresses([raw_text])
    addresses: list[str] = []
    for _name, address in parsed:
        clean = str(address or "").strip()
        if not clean or not _EMAIL_RE.match(clean):
            raise GmailRestError(f"{field} contains an invalid email address.")
        addresses.append(clean)
    if required and not addresses:
        raise GmailRestError(f"{field} is required.")
    return addresses


def _clean_subject(value: str) -> str:
    subject = re.sub(r"\s+", " ", str(value or "")).strip()
    if not subject:
        raise GmailRestError("subject is required.")
    if len(subject) > DRAFT_SUBJECT_CHAR_LIMIT:
        raise GmailRestError(
            f"subject exceeds {DRAFT_SUBJECT_CHAR_LIMIT} characters."
        )
    return subject


def _clean_draft_body(value: str) -> str:
    body = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    if not body.strip():
        raise GmailRestError("body is required.")
    if len(body) > DRAFT_BODY_CHAR_LIMIT:
        raise GmailRestError(
            f"body exceeds {DRAFT_BODY_CHAR_LIMIT} characters."
        )
    return body


def _clean_gmail_id(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise GmailRestError(f"{field} is required.")
    if not _GMAIL_ID_RE.match(clean):
        raise GmailRestError(f"{field} contains invalid characters.")
    return clean


def _clean_label_query(value: str) -> str:
    clean = re.sub(r"\s+", " ", str(value or "")).strip()
    if not clean:
        raise GmailRestError("label is required.")
    if len(clean) > 225:
        raise GmailRestError("label exceeds 225 characters.")
    return clean


def _require_compose_permission() -> None:
    try:
        blob = get_oauth_token(GMAIL_REST_KEY)
    except Exception as exc:
        raise GmailRestError(NOT_CONNECTED_ERROR) from exc
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}
    if not token.get("access_token") and not token.get("refresh_token"):
        raise GmailRestError(NOT_CONNECTED_ERROR)
    if not _token_has_readonly_scope(token):
        raise GmailRestError(PERMISSION_ERROR)
    if not _token_has_compose_scope(token):
        raise GmailRestError(COMPOSE_UPGRADE_ERROR)


def _require_modify_permission() -> None:
    try:
        blob = get_oauth_token(GMAIL_REST_KEY)
    except Exception as exc:
        raise GmailRestError(NOT_CONNECTED_ERROR) from exc
    token = blob.get("token") if isinstance(blob, dict) else None
    token = token if isinstance(token, dict) else {}
    if not token.get("access_token") and not token.get("refresh_token"):
        raise GmailRestError(NOT_CONNECTED_ERROR)
    if not _token_has_readonly_scope(token):
        raise GmailRestError(PERMISSION_ERROR)
    if not _token_has_modify_scope(token):
        raise GmailRestError(MODIFY_UPGRADE_ERROR)


def _confirm_gmail_draft_creation(draft: dict) -> bool:
    """Apply the shared connector write safety gate for Gmail draft creation.

    The stored compatibility key is still named ``mcp_confirm_writes``, but the
    behavior is now connector-wide: OFF asks, ON auto-approves writes.
    """
    return _confirm_gmail_write(
        "gmail_create_draft",
        _draft_confirmation_summary(draft),
    )


def _confirm_gmail_write(tool_name: str, summary: str) -> bool:
    """Apply OSIA's shared connector write safety settings to Gmail writes."""
    try:
        from osia.mcp_client.registry import confirm_connector_write
    except Exception as exc:
        raise GmailRestError(WRITE_CONFIRMATION_REQUIRED_ERROR) from exc
    blocked = confirm_connector_write("gmail", tool_name, summary)
    if blocked is None:
        return True
    if "user declined" in blocked:
        return False
    if "no approver available" in blocked:
        raise GmailRestError(WRITE_CONFIRMATION_REQUIRED_ERROR)
    raise GmailRestError(blocked)


def _draft_confirmation_summary(draft: dict) -> str:
    body = str(draft.get("body") or "")
    preview = re.sub(r"\s+", " ", body).strip()
    if len(preview) > DRAFT_PREVIEW_CHAR_LIMIT:
        preview = preview[: DRAFT_PREVIEW_CHAR_LIMIT - 3].rstrip() + "..."
    parts = [
        "Create Gmail draft",
        f"To: {', '.join(draft.get('to') or [])}",
    ]
    cc = draft.get("cc") or []
    bcc = draft.get("bcc") or []
    if cc:
        parts.append(f"Cc: {', '.join(cc)}")
    if bcc:
        parts.append(f"Bcc: {', '.join(bcc)}")
    parts.extend([
        f"Subject: {draft.get('subject') or ''}",
        f"Body length: {len(body)} characters",
        f"Preview: {preview}" if preview else "Preview: (empty)",
    ])
    return "\n".join(parts)


def _send_draft_confirmation_summary(draft: dict) -> str:
    snippet = _safe_preview(draft.get("snippet"))
    parts = [
        "Send Gmail draft",
        f"Draft id: {draft.get('draftId') or ''}",
        f"To: {draft.get('to') or '(not available)'}",
    ]
    if draft.get("cc"):
        parts.append(f"Cc: {draft.get('cc')}")
    if draft.get("bcc"):
        parts.append("Bcc: present")
    parts.extend([
        f"Subject: {draft.get('subject') or '(no subject)'}",
        f"Snippet: {snippet}" if snippet else "Snippet: (not available)",
    ])
    return "\n".join(parts)


def _message_modify_confirmation_summary(
    action: str,
    message_id: str,
    *,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
) -> str:
    parts = [
        action,
        f"Message id: {message_id}",
    ]
    if add_labels:
        parts.append(f"Add labels: {', '.join(add_labels)}")
    if remove_labels:
        parts.append(f"Remove labels: {', '.join(remove_labels)}")
    return "\n".join(parts)


def _label_modify_confirmation_summary(
    action: str,
    message_id: str,
    label: str,
) -> str:
    return "\n".join([
        action,
        f"Message id: {message_id}",
        f"Label: {label}",
        "Only an exact existing user label will be used.",
    ])


def _safe_preview(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > CONFIRMATION_PREVIEW_CHAR_LIMIT:
        return text[: CONFIRMATION_PREVIEW_CHAR_LIMIT - 3].rstrip() + "..."
    return text


def _message_header_value(message: dict, header_name: str) -> str:
    payload = message.get("payload") if isinstance(message, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    wanted = str(header_name or "").strip().lower()
    for header in payload.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        if name == wanted:
            return _bounded_str(header.get("value"), HEADER_CHAR_LIMIT)
    return ""


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


def _token_has_readonly_scope(token: dict) -> bool:
    scope = token.get("scope") if isinstance(token, dict) else ""
    if not isinstance(scope, str):
        return False
    return GMAIL_READONLY_SCOPE in scope.split()


def _token_has_compose_scope(token: dict) -> bool:
    scope = token.get("scope") if isinstance(token, dict) else ""
    if not isinstance(scope, str):
        return False
    return GMAIL_COMPOSE_SCOPE in scope.split()


def _token_has_modify_scope(token: dict) -> bool:
    scope = token.get("scope") if isinstance(token, dict) else ""
    if not isinstance(scope, str):
        return False
    return GMAIL_MODIFY_SCOPE in scope.split()


def _token_scope_list(token: dict) -> list[str]:
    scope = token.get("scope") if isinstance(token, dict) else ""
    if not isinstance(scope, str):
        return []
    return sorted({part for part in scope.split() if part})


def _gmail_capability_payload(token: dict, *, connected: bool) -> dict:
    scopes = _token_scope_list(token)
    read_only = GMAIL_READONLY_SCOPE in scopes
    compose = GMAIL_COMPOSE_SCOPE in scopes
    modify = GMAIL_MODIFY_SCOPE in scopes
    return {
        "scopes": scopes,
        "capabilities": {
            "readOnly": bool(connected and read_only),
            "compose": bool(connected and compose),
            "modify": bool(connected and modify),
            "composeUpgradeRequired": bool(connected and read_only and not compose),
            "modifyUpgradeRequired": bool(connected and read_only and not modify),
        },
        "readOnlyConnected": bool(connected and read_only),
        "composeEnabled": bool(connected and compose),
        "modifyEnabled": bool(connected and modify),
        "composeUpgradeRequired": bool(connected and read_only and not compose),
        "modifyUpgradeRequired": bool(connected and read_only and not modify),
    }


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
    group.add_argument("--upgrade-compose", action="store_true",
                       help="run OAuth re-consent for Gmail draft creation")
    group.add_argument("--upgrade-modify", action="store_true",
                       help="run OAuth re-consent for Gmail mailbox actions")
    group.add_argument("--status", action="store_true",
                       help="show safe Gmail connection and capability status")
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
        elif args.upgrade_compose:
            if args.no_browser:
                raise GmailRestError("--upgrade-compose requires browser OAuth.")
            result = gmail_upgrade_compose_permissions()
        elif args.upgrade_modify:
            if args.no_browser:
                raise GmailRestError("--upgrade-modify requires browser OAuth.")
            result = gmail_upgrade_modify_permissions()
        elif args.status:
            result = gmail_connection_status(validate=False)
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

    try:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

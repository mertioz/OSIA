"""Provider-specific image-input validation and wire encoding.

This module is the single fail-closed decision point for native model vision.
It deliberately uses exact model metadata instead of guessing from model names.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass


_ALL_OSIA_IMAGE_MIMES = frozenset({
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})

_ADAPTERS = {
    "chatgpt_account": ("app_server_local_image", _ALL_OSIA_IMAGE_MIMES),
    "google": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png", "image/webp"}),
    ),
    "groq": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
    "mistral": (
        "mistral_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
    "openai": ("openai_image_url", _ALL_OSIA_IMAGE_MIMES),
    "xai": (
        "openai_image_url",
        frozenset({"image/jpeg", "image/png"}),
    ),
}


class ImageInputError(RuntimeError):
    """A safe user-facing image-input rejection raised before inference."""


@dataclass(frozen=True)
class ImageInputDecision:
    provider: str
    model: str
    model_known: bool
    model_supports_vision: bool
    adapter_supports_images: bool
    account_path_permits_images: bool
    formats_accepted: bool
    encoding: str | None
    usable: bool
    reason: str


@dataclass(frozen=True)
class DecodedImage:
    mime: str
    data: bytes


@dataclass(frozen=True)
class MediaExportDecision:
    """Fail-closed decision for an optional provider-side image description."""

    provider_capable: bool
    exact_source_authorized: bool
    media_export_authorized: bool
    canonical_reencoded: bool
    same_provider: bool
    usable: bool
    reason: str


def text_content(content) -> str:
    """Return only textual parts from one OpenAI-style message content value."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
        and part.get("text")
    )


def image_parts(content) -> list[dict]:
    if not isinstance(content, list):
        return []
    return [
        part for part in content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]


def has_image_content(content) -> bool:
    return bool(image_parts(content))


def decode_image_data_url(value, *, maximum_bytes: int | None = None) -> DecodedImage:
    """Decode one bounded data URL and verify its declared MIME by magic bytes."""

    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.startswith("data:image/"):
        raise ImageInputError(
            "ERROR: an attached image is not an inline OSIA image; "
            "no provider request was sent."
        )
    header, separator, payload = value.partition(",")
    if not separator or not header.endswith(";base64"):
        raise ImageInputError(
            "ERROR: an attached image has an invalid data encoding; "
            "no provider request was sent."
        )
    mime = header[5:-7].lower()
    if mime not in _ALL_OSIA_IMAGE_MIMES:
        raise ImageInputError(
            "ERROR: an attached image has an unsupported MIME type; "
            "no provider request was sent."
        )
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageInputError(
            "ERROR: an attached image has invalid base64 data; "
            "no provider request was sent."
        ) from exc
    if not data:
        raise ImageInputError(
            "ERROR: an attached image is empty; no provider request was sent."
        )
    if maximum_bytes is not None and len(data) > maximum_bytes:
        raise ImageInputError(
            "ERROR: an attached image exceeds the provider size limit; "
            "no provider request was sent."
        )
    detected = _magic_mime(data)
    if detected != mime:
        raise ImageInputError(
            "ERROR: an attached image MIME type does not match its bytes; "
            "no provider request was sent."
        )
    if mime == "image/gif":
        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                if int(getattr(image, "n_frames", 1)) != 1:
                    raise ImageInputError(
                        "ERROR: animated GIF image input is not supported; "
                        "no provider request was sent."
                    )
        except ImageInputError:
            raise
        except Exception as exc:
            raise ImageInputError(
                "ERROR: an attached GIF image is invalid; "
                "no provider request was sent."
            ) from exc
    return DecodedImage(mime=mime, data=data)


def _magic_mime(data: bytes) -> str | None:
    prefix = data[:16]
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
        return "image/webp"
    return None


def _account_model_metadata(model: str) -> dict | None:
    try:
        from osia.chatgpt_account.provider import get_account_provider

        provider = get_account_provider()
        models = provider.cached_models()
        if not any(str(item.get("id") or "") == model for item in models):
            models = provider.list_models()
        return next(
            (item for item in models if str(item.get("id") or "") == model),
            None,
        )
    except Exception:
        return None


def _known_model_vision(provider: str, model: str) -> tuple[bool, bool, bool]:
    if provider == "chatgpt_account":
        metadata = _account_model_metadata(model)
        if metadata is None:
            return False, False, False
        modalities = {
            str(item).lower() for item in metadata.get("inputModalities") or []
        }
        permitted = "image" in modalities
        return True, permitted, permitted

    from osia.models import HARDCODED_MODELS, VISION_MODELS

    metadata = next(
        (
            item for item in (*HARDCODED_MODELS, *VISION_MODELS)
            if item.get("provider") == provider and item.get("id") == model
        ),
        None,
    )
    if metadata is None:
        return False, False, True
    return True, bool(metadata.get("vision")), True


def image_input_decision(
    provider: str,
    model: str,
    mime_types=(),
) -> ImageInputDecision:
    """Resolve all five capability dimensions for one provider/model/path."""

    provider = str(provider or "").lower()
    model = str(model or "")
    model_known, model_vision, account_permits = _known_model_vision(provider, model)
    adapter = _ADAPTERS.get(provider)
    adapter_supported = adapter is not None
    encoding = adapter[0] if adapter else None
    accepted = adapter[1] if adapter else frozenset()
    formats_ok = all(str(mime).lower() in accepted for mime in mime_types)

    if not model_known:
        reason = "the model capability is unknown"
    elif not model_vision:
        reason = "the selected model is text-only"
    elif not adapter_supported:
        reason = "the current provider adapter has no verified image encoding"
    elif not account_permits:
        reason = "the selected account path does not permit image input"
    elif not formats_ok:
        reason = "the attachment format is not accepted by this provider path"
    else:
        reason = "supported"

    usable = bool(
        model_known
        and model_vision
        and adapter_supported
        and account_permits
        and formats_ok
    )
    return ImageInputDecision(
        provider=provider,
        model=model,
        model_known=model_known,
        model_supports_vision=model_vision,
        adapter_supports_images=adapter_supported,
        account_path_permits_images=account_permits,
        formats_accepted=formats_ok,
        encoding=encoding,
        usable=usable,
        reason=reason,
    )


def media_export_decision(
    provider: str,
    model: str,
    mime_type: str,
    *,
    exact_source_authorized: bool,
    media_export_authorized: bool,
    canonical_reencoded: bool,
    same_provider: bool,
) -> MediaExportDecision:
    """Require every WOR-188 export condition; local OCR needs none of them."""

    capability = image_input_decision(provider, model, (mime_type,))
    checks = (
        (capability.usable, capability.reason),
        (exact_source_authorized, "the exact image source is not authorized"),
        (media_export_authorized, "media export permission is not authorized"),
        (canonical_reencoded, "the image was not canonically metadata-stripped"),
        (same_provider, "silent provider switching is forbidden"),
    )
    reason = "supported"
    for allowed, failure in checks:
        if not allowed:
            reason = failure
            break
    return MediaExportDecision(
        provider_capable=capability.usable,
        exact_source_authorized=bool(exact_source_authorized),
        media_export_authorized=bool(media_export_authorized),
        canonical_reencoded=bool(canonical_reencoded),
        same_provider=bool(same_provider),
        usable=all(item[0] for item in checks),
        reason=reason,
    )


def ensure_image_input_supported(provider: str, model: str, content) -> ImageInputDecision | None:
    """Validate every image and reject unusable paths before provider execution."""

    parts = image_parts(content)
    if not parts:
        return None
    decoded = [decode_image_data_url(part.get("image_url")) for part in parts]
    decision = image_input_decision(provider, model, [item.mime for item in decoded])
    if not decision.usable:
        raise ImageInputError(
            "ERROR: the current provider/model path "
            f"({provider or 'unknown'} / {model or 'unknown'}) cannot analyze "
            f"the attached image: {decision.reason}. No provider request was sent."
        )
    return decision


def adapt_messages_for_provider(messages: list, provider: str) -> list:
    """Translate only providers whose verified image chunk differs from OpenAI."""

    if str(provider or "").lower() != "mistral":
        return messages
    changed = False
    wire = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            wire.append(message)
            continue
        parts = []
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, dict):
                    part = dict(part)
                    part["image_url"] = image_url.get("url", "")
                    changed = True
            parts.append(part)
        copied = dict(message)
        copied["content"] = parts
        wire.append(copied)
    return wire if changed else messages

"""File-backed attachment drafts for the desktop message composer.

The mobile transport owns its separate, bounded upload policy in
``osia.attachments``.  Desktop selection stores metadata only and applies the
selected provider/model policy immediately before a turn is accepted.
"""

from __future__ import annotations

import mimetypes
import os
import stat
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from osia.attachments import detect_image_mime, sanitize_display_name
from osia.image_inputs import (
    ImageArtifact,
    ImageSourceType,
    PROVIDER_IMAGE_POLICIES,
    ProviderImagePolicy,
    content_with_images,
    create_image_artifact,
    image_input_decision,
)


_MIB = 1024 * 1024
_PREFIX_BYTES = 16
_SUPPORTED_IMAGE_MIMES = frozenset({
    "image/gif", "image/jpeg", "image/png", "image/webp",
})


class DesktopAttachmentError(RuntimeError):
    """One safe attachment failure that never includes a source path."""

    def __init__(
        self,
        code: str,
        *,
        attachment_id: str | None = None,
        display_name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit_bytes: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.attachment_id = attachment_id
        self.display_name = display_name
        self.provider = provider
        self.model = model
        self.limit_bytes = limit_bytes


@dataclass
class DraftAttachment:
    """Metadata-only reference retained by one in-memory desktop draft."""

    draft_id: str
    source_path: Path = field(repr=False)
    display_name: str
    detected_mime: str
    size_bytes: int
    attachment_type: str
    logical_order: int
    modified_ns: int
    device: int
    inode: int
    byte_proved_image: bool = False
    validation_state: str = "pending"
    error_code: str | None = None


@dataclass(frozen=True)
class PreparedDesktopTurn:
    """Provider-ready content and its path-free conversation representation."""

    provider_content: str | list[dict]
    display_text: str
    attachment_ids: tuple[str, ...]
    image_references: tuple[dict, ...] = ()


def readable_file_size(size_bytes: int) -> str:
    size = max(0, int(size_bytes))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{size} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def _safe_mime(display_name: str) -> str:
    guessed, _encoding = mimetypes.guess_type(display_name, strict=False)
    if isinstance(guessed, str) and 0 < len(guessed) <= 127:
        return guessed.lower()
    return "application/octet-stream"


def _read_prefix(source: Path, *, item: DraftAttachment | None = None) -> bytes:
    try:
        with source.open("rb") as handle:
            return handle.read(_PREFIX_BYTES)
    except OSError as exc:
        raise DesktopAttachmentError(
            "unreadable_file",
            attachment_id=item.draft_id if item else None,
            display_name=item.display_name if item else sanitize_display_name(source.name),
        ) from exc


def stage_attachment(path: str | Path, logical_order: int) -> DraftAttachment:
    """Stage one regular file using stat metadata and a 16-byte MIME read."""

    candidate = Path(path).expanduser()
    display_name = sanitize_display_name(candidate.name)
    try:
        source = candidate.resolve(strict=True)
        details = source.stat()
    except (OSError, RuntimeError) as exc:
        raise DesktopAttachmentError(
            "missing_file", display_name=display_name,
        ) from exc
    if not stat.S_ISREG(details.st_mode):
        raise DesktopAttachmentError(
            "not_regular_file", display_name=display_name,
        )
    if details.st_size <= 0:
        raise DesktopAttachmentError("empty_file", display_name=display_name)
    if details.st_size > sys.maxsize:
        raise DesktopAttachmentError("resource_limit", display_name=display_name)

    prefix = _read_prefix(source)
    image_mime = detect_image_mime(prefix)
    declared_mime = _safe_mime(display_name)
    is_image = bool(
        image_mime or declared_mime in _SUPPORTED_IMAGE_MIMES
    )
    return DraftAttachment(
        draft_id=uuid.uuid4().hex,
        source_path=source,
        display_name=display_name,
        detected_mime=image_mime or declared_mime,
        size_bytes=int(details.st_size),
        attachment_type="image" if is_image else "file",
        logical_order=int(logical_order),
        modified_ns=int(details.st_mtime_ns),
        device=int(details.st_dev),
        inode=int(details.st_ino),
        byte_proved_image=bool(image_mime),
    )


def _validate_metadata(item: DraftAttachment) -> None:
    try:
        details = item.source_path.stat()
    except OSError as exc:
        raise DesktopAttachmentError(
            "missing_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        ) from exc
    if not stat.S_ISREG(details.st_mode):
        raise DesktopAttachmentError(
            "not_regular_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        )
    identity_changed = (
        (item.device and int(details.st_dev) != item.device)
        or (item.inode and int(details.st_ino) != item.inode)
    )
    if (
        int(details.st_size) != item.size_bytes
        or int(details.st_mtime_ns) != item.modified_ns
        or identity_changed
    ):
        raise DesktopAttachmentError(
            "changed_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        )
    prefix = _read_prefix(item.source_path, item=item)
    image_mime = detect_image_mime(prefix)
    declared_mime = _safe_mime(item.display_name)
    current_type = (
        "image"
        if image_mime or declared_mime in _SUPPORTED_IMAGE_MIMES else
        "file"
    )
    current_mime = image_mime or declared_mime
    if (
        current_type != item.attachment_type
        or current_mime != item.detected_mime
        or (
            item.attachment_type == "image"
            and bool(image_mime) != bool(item.byte_proved_image)
        )
    ):
        raise DesktopAttachmentError(
            "changed_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        )


def _encoded_size(size_bytes: int, mime: str) -> int:
    encoded = 4 * ((int(size_bytes) + 2) // 3)
    return encoded + len(f"data:{mime};base64,")


def _preflight_image_policy(
    images: list[DraftAttachment], provider: str, model: str,
) -> None:
    if not images:
        return
    provider = str(provider or "").lower()
    decision = image_input_decision(
        provider, model, [item.detected_mime for item in images],
    )
    if not decision.usable:
        if not decision.model_known or not decision.model_supports_vision:
            code = "unsupported_model"
        elif not decision.adapter_supports_images or not decision.account_path_permits_images:
            code = "unsupported_provider"
        else:
            code = "unsupported_mime"
        raise DesktopAttachmentError(
            code,
            attachment_id=images[0].draft_id,
            display_name=images[0].display_name,
            provider=provider,
            model=model,
        )

    policy = PROVIDER_IMAGE_POLICIES.get(provider, ProviderImagePolicy())
    if policy.maximum_images is not None and len(images) > policy.maximum_images:
        raise DesktopAttachmentError(
            "too_many_images",
            attachment_id=images[policy.maximum_images].draft_id,
            display_name=images[policy.maximum_images].display_name,
            provider=provider,
            model=model,
            limit_bytes=policy.maximum_images,
        )
    if policy.maximum_image_bytes is not None:
        oversized = next(
            (item for item in images if item.size_bytes > policy.maximum_image_bytes),
            None,
        )
        if oversized is not None:
            raise DesktopAttachmentError(
                "provider_size_limit",
                attachment_id=oversized.draft_id,
                display_name=oversized.display_name,
                provider=provider,
                model=model,
                limit_bytes=policy.maximum_image_bytes,
            )
    if policy.maximum_encoded_payload_bytes is not None:
        encoded_total = sum(
            _encoded_size(item.size_bytes, item.detected_mime) for item in images
        )
        if encoded_total > policy.maximum_encoded_payload_bytes:
            raise DesktopAttachmentError(
                "provider_size_limit",
                attachment_id=images[0].draft_id,
                display_name=images[0].display_name,
                provider=provider,
                model=model,
                limit_bytes=policy.maximum_encoded_payload_bytes,
            )


def _available_memory() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def _preflight_memory(images: list[DraftAttachment]) -> None:
    if not images:
        return
    raw_total = sum(item.size_bytes for item in images)
    encoded_total = sum(
        _encoded_size(item.size_bytes, item.detected_mime) for item in images
    )
    # The current adapters retain raw bytes and a Unicode base64 data URL while
    # constructing the request.  Reject only when the measured machine resource
    # cannot safely cover that representation; this is not a product size cap.
    estimated_peak = raw_total + (2 * encoded_total) + (8 * _MIB)
    if estimated_peak > sys.maxsize:
        raise DesktopAttachmentError(
            "resource_limit",
            attachment_id=images[0].draft_id,
            display_name=images[0].display_name,
        )
    available = _available_memory()
    if available is not None and estimated_peak > available // 3:
        raise DesktopAttachmentError(
            "resource_limit",
            attachment_id=images[0].draft_id,
            display_name=images[0].display_name,
        )


def _read_image_snapshot(item: DraftAttachment) -> bytes:
    try:
        with item.source_path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise DesktopAttachmentError(
            "unreadable_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        ) from exc
    current_image_mime = detect_image_mime(data[:_PREFIX_BYTES])
    if (
        len(data) != item.size_bytes
        or int(before.st_size) != item.size_bytes
        or int(after.st_size) != item.size_bytes
        or int(before.st_mtime_ns) != item.modified_ns
        or int(after.st_mtime_ns) != item.modified_ns
        or (
            item.byte_proved_image
            and current_image_mime != item.detected_mime
        )
        or (not item.byte_proved_image and current_image_mime is not None)
    ):
        raise DesktopAttachmentError(
            "changed_file",
            attachment_id=item.draft_id,
            display_name=item.display_name,
        )
    return data


def prepare_desktop_turn(
    prompt: str,
    attachments: list[DraftAttachment] | tuple[DraftAttachment, ...],
    *,
    provider: str,
    model: str,
    owner_id: str | None = None,
    permission_epoch: int | None = None,
) -> PreparedDesktopTurn:
    """Validate and prepare every item before any provider execution begins."""

    ordered = sorted(list(attachments), key=lambda item: item.logical_order)
    if not ordered:
        raise DesktopAttachmentError("empty_draft")
    if len({item.draft_id for item in ordered}) != len(ordered):
        raise DesktopAttachmentError("invalid_draft")
    for item in ordered:
        _validate_metadata(item)

    images = [item for item in ordered if item.attachment_type == "image"]
    files = [item for item in ordered if item.attachment_type != "image"]
    _preflight_image_policy(images, provider, model)
    _preflight_memory(images)

    image_artifacts: list[ImageArtifact] = []
    for item in images:
        try:
            image_artifacts.append(create_image_artifact(
                _read_image_snapshot(item),
                item.detected_mime,
                ImageSourceType.USER_UPLOAD,
                maximum_bytes=(
                    PROVIDER_IMAGE_POLICIES.get(
                        str(provider or "").lower(), ProviderImagePolicy(),
                    ).maximum_image_bytes
                ),
            ))
        except DesktopAttachmentError:
            raise
        except Exception as exc:
            raise DesktopAttachmentError(
                "invalid_image",
                attachment_id=item.draft_id,
                display_name=item.display_name,
                provider=provider,
                model=model,
            ) from exc

    document_capabilities = []
    if files:
        from osia.document_sources import DocumentSourceError, get_source_store

        try:
            for item in files:
                if owner_id:
                    capability = get_source_store().create(
                        item.source_path,
                        owner_id=owner_id,
                        source_kind="desktop_attachment",
                        display_name=item.display_name,
                        declared_mime=item.detected_mime,
                        permission_epoch=permission_epoch,
                    )
                else:
                    capability = get_source_store().create_for_current_owner(
                        item.source_path,
                        source_kind="desktop_attachment",
                        display_name=item.display_name,
                        declared_mime=item.detected_mime,
                        permission_epoch=permission_epoch,
                    )
                document_capabilities.append(capability)
        except DocumentSourceError as exc:
            raise DesktopAttachmentError(
                exc.category,
                attachment_id=item.draft_id,
                display_name=item.display_name,
            ) from exc

    text_parts = []
    if files:
        source_lines = "\n".join(
            f"{ordered.index(item) + 1}. source_ref={capability['source_ref']} "
            f"type={capability['type_label']} state={capability['state']}"
            + (f" category={capability['category']}" if capability.get("category") else "")
            for item, capability in zip(files, document_capabilities)
        )
        text_parts.append(
            "[Documents available through read_document using only each opaque "
            "source_ref, followed by query_document. Never request or infer a "
            "local path:\n"
            + source_lines
            + "\n]"
        )
    clean_prompt = str(prompt or "").strip()
    if clean_prompt:
        text_parts.append(clean_prompt)
    elif files and not images:
        text_parts.append("Analyze the attached document or documents.")
    elif images:
        text_parts.append("Analyze the attached image or images.")
    provider_text = "\n\n".join(text_parts)

    provider_content: str | list[dict] = (
        content_with_images(
            provider_text,
            image_artifacts,
            owner_id=str(owner_id or ""),
        )
        if image_artifacts else provider_text
    )

    display_lines = [clean_prompt] if clean_prompt else []
    display_lines.extend(f"📎 {item.display_name}" for item in ordered)
    return PreparedDesktopTurn(
        provider_content=provider_content,
        display_text="\n".join(display_lines),
        attachment_ids=tuple(item.draft_id for item in ordered),
        image_references=tuple(
            artifact.safe_reference() for artifact in image_artifacts
        ),
    )


__all__ = [
    "DesktopAttachmentError",
    "DraftAttachment",
    "PreparedDesktopTurn",
    "PROVIDER_IMAGE_POLICIES",
    "ProviderImagePolicy",
    "prepare_desktop_turn",
    "readable_file_size",
    "stage_attachment",
]

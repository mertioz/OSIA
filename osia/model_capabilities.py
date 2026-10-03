"""Route-bound effective model capability authority.

Ordinary providers use exact static registry metadata. ChatGPT Account uses the
current App Server model catalogue. No capability is inferred from a model
name or family.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class EffectiveRouteCapabilities:
    provider_id: str
    model_id: str
    metadata_known: bool
    input_modalities: tuple[str, ...]
    vision_supported: bool | None
    reasoning_controls: tuple[str, ...]
    source_authority: str

    def safe_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "metadata_known": self.metadata_known,
            "input_modalities": list(self.input_modalities),
            "vision_supported": self.vision_supported,
            "reasoning_controls": list(self.reasoning_controls),
            "source_authority": self.source_authority,
        }


def _exact_account_metadata(model_id: str) -> Mapping[str, Any] | None:
    try:
        from osia.chatgpt_account.provider import get_account_provider

        provider = get_account_provider()
        models = provider.cached_models()
        metadata = next(
            (item for item in models if str(item.get("id") or "") == model_id),
            None,
        )
        if metadata is None:
            models = provider.list_models(force=True)
            metadata = next(
                (item for item in models if str(item.get("id") or "") == model_id),
                None,
            )
        return metadata
    except Exception:
        return None


def effective_route_capabilities(
    provider_id: str,
    model_id: str,
    *,
    model_metadata: Mapping[str, Any] | None = None,
) -> EffectiveRouteCapabilities:
    provider = str(provider_id or "").strip().casefold()
    model = str(model_id or "").strip()
    metadata = model_metadata if isinstance(model_metadata, Mapping) else None
    source = ""
    if metadata is not None:
        metadata_model = str(metadata.get("id") or metadata.get("model") or model)
        metadata_provider = str(metadata.get("provider") or provider).casefold()
        if metadata_model != model or metadata_provider != provider:
            metadata = None
        else:
            source = "explicit_route_metadata"
    if metadata is None and provider == "chatgpt_account":
        metadata = _exact_account_metadata(model)
        if metadata is not None:
            source = "app_server_model_list"
    elif metadata is None and provider != "chatgpt_account":
        try:
            from osia.models import HARDCODED_MODELS

            metadata = next(
                (
                    item for item in HARDCODED_MODELS
                    if str(item.get("provider") or "").casefold() == provider
                    and str(item.get("id") or "") == model
                ),
                None,
            )
        except Exception:
            metadata = None
        if metadata is not None:
            source = "osia_models_exact_registry"

    modalities: tuple[str, ...] = ()
    vision: bool | None = None
    reasoning: tuple[str, ...] = ()
    if metadata is not None:
        raw_modalities = (
            metadata.get("inputModalities")
            or metadata.get("input_modalities")
            or ()
        )
        if isinstance(raw_modalities, (list, tuple)):
            modalities = tuple(dict.fromkeys(
                str(item).casefold()
                for item in raw_modalities
                if str(item).casefold() in {"text", "image", "audio"}
            ))
        if isinstance(metadata.get("vision"), bool):
            vision = bool(metadata.get("vision"))
            if not modalities:
                modalities = ("text", "image") if vision else ("text",)
        elif modalities:
            vision = "image" in modalities
        efforts = metadata.get("supportedReasoningEfforts")
        if isinstance(efforts, (list, tuple)):
            reasoning = tuple(
                str(item.get("id") if isinstance(item, Mapping) else item)
                for item in efforts
                if str(
                    item.get("id") if isinstance(item, Mapping) else item
                )
            )
    return EffectiveRouteCapabilities(
        provider_id=provider,
        model_id=model,
        metadata_known=metadata is not None,
        input_modalities=modalities,
        vision_supported=vision,
        reasoning_controls=reasoning,
        source_authority=source or "unknown",
    )


__all__ = [
    "EffectiveRouteCapabilities",
    "effective_route_capabilities",
]

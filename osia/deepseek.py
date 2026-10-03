"""DeepSeek selection compatibility at settings/runtime loading boundaries."""

from __future__ import annotations

import copy


MODEL_ID = "deepseek-flash"
MODEL_LABEL = "DeepSeek V4.1 Flash"
# Historical API IDs, never selectable. Do not infer a provider from local R1 IDs.
LEGACY_API_IDS = frozenset({
    "deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro",
    "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
})


def migrate_model_id(provider, model):
    """Keep provider identity; collapse old DeepSeek selections to its sole model."""
    if not isinstance(model, str) or not model:
        return model
    provider_id = str(provider or "").strip().lower()
    if (provider_id == "deepseek" and model.startswith("deepseek-")) or (
        not provider_id and model in LEGACY_API_IDS
    ):
        return MODEL_ID
    return model


def migrate_settings(settings: dict) -> dict:
    """Return migrated selections without writing on read or reordering chains.

    Old reasoning keys remain historical data. A canonical override wins; else
    the selected then hierarchy model's explicit override is copied.  The
    legacy ``autonomous_model`` key is preserved untouched as historical data.
    """
    result = copy.deepcopy(settings)
    previous = []
    for key in ("selected_model",):
        old = result.get(key)
        new = migrate_model_id(result.get(key + "_provider"), old)
        if new != old:
            result[key] = new
            previous.append(old)
    hierarchy = result.get("ai_hierarchy")
    if isinstance(hierarchy, dict):
        for category, chain in hierarchy.items():
            if not isinstance(chain, list):
                continue
            migrated = []
            for entry in chain:
                if isinstance(entry, str):
                    provider, separator, model = entry.partition(":")
                    if separator:
                        new = migrate_model_id(provider, model)
                        if new != model:
                            previous.append(model)
                            entry = provider + ":" + new
                migrated.append(entry)
            hierarchy[category] = migrated
    reasoning = result.get("reasoning")
    levels = reasoning.get("models") if isinstance(reasoning, dict) else None
    if isinstance(levels, dict) and MODEL_ID not in levels:
        for old in previous:
            if old in levels:
                levels[MODEL_ID] = levels[old]
                break
    return result

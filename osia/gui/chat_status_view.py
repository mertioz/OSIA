"""Small, display-only projection of the PC's Chat status payload."""
from __future__ import annotations

import math


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _tokens(value) -> str:
    number = _number(value)
    return f"{int(number):,}".replace(",", " ") if number is not None else "—"


def _usd(value, *, french: bool) -> str:
    number = _number(value)
    if number is None:
        return "indisponible" if french else "unavailable"
    if 0 < number < 0.00000001:
        return "< 0.00000001 USD"
    return f"{number:.8f}".rstrip("0").rstrip(".") + " USD"


def status_lines(payload: dict, locale: str = "en") -> list[str]:
    """Render only model, context and the relevant quota or estimated API usage."""
    fr = locale.lower().startswith("fr")
    unknown = "indisponible" if fr else "unavailable"
    model = payload.get("model") or {}
    label = str(model.get("label") or model.get("id") or unknown)
    lines = [("Modèle : " if fr else "Model: ") + label]
    context = payload.get("context")
    if isinstance(context, dict):
        approximate = "≈ " if context.get("estimated") is not False else ""
        lines.append(("Contexte : " if fr else "Context: ") + approximate
                     + _tokens(context.get("used")) + " / "
                     + _tokens(context.get("maximum")) + (" jetons" if fr else " tokens"))
    else:
        lines.append(("Contexte : " if fr else "Context: ") + unknown)
    if payload.get("connection") == "account":
        quota = payload.get("quota") or {}
        windows = quota.get("windows") if quota.get("state") == "available" else []
        visible = []
        for window in windows or []:
            remaining = _number(window.get("remaining_percent"))
            if remaining is not None and remaining <= 100:
                visible.append(("Quota restant" if fr else "Remaining quota")
                               + f" ({window.get('label') or '—'}) : {remaining:g} %")
        lines.extend(visible or [("Quota restant : " if fr else "Remaining quota: ") + unknown])
        return lines
    usage = payload.get("usage") or {}
    lines.append(("Jetons utilisés (estimation) : ≈ " if fr else "Tokens used (estimate): ≈ ")
                 + _tokens(usage.get("total_tokens")))
    local = payload.get("connection") == "local"
    lines.append(("Coût estimé : " if fr else "Estimated cost: ")
                 + (("sans facturation API locale" if fr else "no local API billing") if local else
                    ("≈ " if _number(usage.get("cost_usd")) is not None else "")
                    + _usd(usage.get("cost_usd"), french=fr)))
    models = usage.get("models") or []
    for entry in models:
        if len(models) > 1:
            item_model = entry.get("model") or {}
            lines.append(str(item_model.get("label") or item_model.get("id") or unknown)
                         + " · ≈ " + _tokens(entry.get("total_tokens"))
                         + (" jetons · " if fr else " tokens · ")
                         + ("—" if local else _usd(entry.get("cost_usd"), french=fr)))
        if local:
            continue
    if not local:
        lines.append("Estimation du texte visible ; usage outil/raisonnement, remises de cache et frais supplémentaires non inclus." if fr
                     else "Visible-text estimate; unrecorded tool/reasoning usage, cache discounts and extra fees are not included.")
    return lines

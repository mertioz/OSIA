"""Documented discovery-only contracts, verified 2026-10-20.

Source: https://developers.openai.com/api/docs/models/<model-id>
These records supplement capability-poor /models responses. They never add
offline picker entries or prove account entitlement. Snapshots are explicit
documented IDs, not guessed suffixes. Unknown efforts retain provider defaults.
"""

# id, context, vision, streaming, protocol, published snapshots
_CONTRACTS = [
    ("gpt-4o", 128000, True, True, "chat_completions", ["gpt-4o-2024-11-20","gpt-4o-2024-08-06","gpt-4o-2024-05-13"]),
    ("gpt-4o-mini", 128000, True, True, "chat_completions", ["gpt-4o-mini-2024-07-18"]),
    ("gpt-5", 400000, True, True, "responses", ["gpt-5-2025-08-07"]),
    ("gpt-5-mini", 400000, True, True, "responses", ["gpt-5-mini-2025-08-07"]),
    ("gpt-5-nano", 400000, True, True, "responses", ["gpt-5-nano-2025-08-07"]),
    ("gpt-5.1", 400000, True, True, "responses", ["gpt-5.1-2025-11-13"]),
    ("gpt-5.2", 400000, True, True, "responses", ["gpt-5.2-2025-12-11"]),
    ("gpt-5.4-nano", 400000, True, True, "responses", ["gpt-5.4-nano-2026-03-17"]),
    ("gpt-daybreak-blue-latest", 1050000, True, True, "responses", []),
    ("gpt-daybreak-red-latest", 400000, True, True, "responses", []),
    ("gpt-5-pro", 400000, True, True, "responses", ["gpt-5-pro-2025-10-06"]),
    ("gpt-5.2-pro", 400000, True, True, "responses", ["gpt-5.2-pro-2025-12-11"]),
    ("gpt-5.4-pro", 1050000, True, True, "responses", ["gpt-5.4-pro-2026-03-05"]),
    ("gpt-5.5-pro", 1050000, True, False, "responses", ["gpt-5.5-pro-2026-04-23"]),
    ("o3", 200000, True, True, "responses", ["o3-2025-04-16"]),
    ("o3-mini", 200000, False, True, "responses", ["o3-mini-2025-01-31"]),
    ("o4-mini", 200000, True, True, "responses", ["o4-mini-2025-04-16"]),
    ("o1", 200000, True, True, "responses", ["o1-2024-12-17"]),
]

DISCOVERY_CONTRACTS = {}
SNAPSHOT_BASES = {
    "gpt-5.5-2026-04-23": "gpt-5.5",
    "gpt-5.4-2026-03-05": "gpt-5.4",
    "gpt-5.4-mini-2026-03-17": "gpt-5.4-mini",
    "gpt-4.1-2025-04-14": "gpt-4.1",
    "gpt-4.1-mini-2025-04-14": "gpt-4.1-mini",
    "gpt-4.1-nano-2025-04-14": "gpt-4.1-nano"
}
for _id, _context, _vision, _streaming, _protocol, _snapshots in _CONTRACTS:
    for _model_id in [_id, *_snapshots]:
        DISCOVERY_CONTRACTS[_model_id] = {
            "id": _model_id, "provider": "openai", "label": _model_id,
            "vision": _vision, "streaming": _streaming, "tool_calls": True,
            "context_window": _context, "api_protocol": _protocol,
            "request_reasoning_summary": False,
        }

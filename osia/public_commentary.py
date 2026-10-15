"""Public assistant text and native-call assembly; no private reasoning feed."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

_LOG = logging.getLogger(__name__)


def public_assistant_text(value) -> str:
    """Mistral may return typed chunks where compatible APIs return a string."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in value
        if isinstance(part, dict) and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


class PublicCommentaryStream:
    """Classify ordinary text only after this response proves a tool context.

    Before that boundary text could be a final answer. A stream without calls
    never emits commentary, including a retrospective after earlier tools.
    Textual call syntax is parsed before private/markup stripping. During an
    unfinished stream, only the prefix before possible markup/JSON is safe.
    """

    def __init__(self, *, provider, model, turn_id, round_id, parse, clean, emit, clean_live=None):
        self.provider = provider
        self.model = model
        self.turn_id = turn_id
        self.round_id = round_id
        self.parse = parse
        self.clean = clean
        self.clean_live = clean_live
        self.emit = emit
        self.text = ""
        self.calls = {}
        self.tool_context = False
        self.emitted = ""
        self.order = 0
        self.started = time.monotonic()

    @property
    def message(self):
        return SimpleNamespace(content=self.text, tool_calls=[
            SimpleNamespace(id=value["id"], type="function", function=SimpleNamespace(
                name=value["name"], arguments=value["arguments"],
            )) for _, value in sorted(self.calls.items())
        ])

    def observe(self, delta):
        text = public_assistant_text(getattr(delta, "content", None))
        self.text += text
        for call in getattr(delta, "tool_calls", None) or ():
            index = getattr(call, "index", None)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                continue
            value = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            function = getattr(call, "function", None)
            for key, fragment in (
                ("id", getattr(call, "id", None)),
                ("name", getattr(function, "name", None)),
                ("arguments", getattr(function, "arguments", None)),
            ):
                if isinstance(fragment, str):
                    value[key] += fragment
            # A native tool delta supplies semantics before its JSON is whole.
            self.tool_context = True
        if not self.tool_context and text and self.parse(self.text, self.message):
            self.tool_context = True
        self._publish(complete=False)
        self._diagnose("delta", len(text))
        return text

    def finish(self):
        self._publish(complete=True)
        self._diagnose("response_complete", 0)

    def _publish(self, *, complete):
        if not self.tool_context:
            return
        candidate = self.text
        if not complete and self.clean_live is None:
            # Never expose fragmented tool arguments, DSML or a partial <think>.
            boundaries = [candidate.find(char) for char in ("<", "{", "`")]
            boundaries = [index for index in boundaries if index >= 0]
            if boundaries:
                candidate = candidate[:min(boundaries)]
        wording = (self.clean_live(candidate) if not complete and self.clean_live
                   else self.clean(candidate))
        if wording and wording != self.emitted:
            self.emit(wording, self.round_id)
            self.emitted = wording
            self._diagnose("commentary_emitted", len(wording))

    def _diagnose(self, boundary, chars):
        self.order += 1
        # Opt-in DEBUG metadata only: no prompts, text, arguments or reasoning.
        _LOG.debug(
            "thinking_timeline provider=%s model=%s turn=%s round=%s order=%s "
            "elapsed_ms=%s boundary=%s public_chars=%s tool_context=%s "
            "commentary_present=%s classification=%s",
            self.provider, self.model, self.turn_id, self.round_id, self.order,
            round((time.monotonic() - self.started) * 1000), boundary, chars,
            self.tool_context, bool(self.emitted),
            ("tool_round_public" if self.emitted else "empty_or_filtered"
             if self.tool_context else "pending_or_final_text"),
        )

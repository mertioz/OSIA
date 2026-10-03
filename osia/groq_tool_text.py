"""Narrow compatibility for the Groq bash dialect captured in WOR-305."""
import json
import re


_BLOCK = re.compile(r"<tool_call\s*>(.*?)(?:</tool_call\s*>|\Z)", re.DOTALL)
_CALL = re.compile(
    r"\s*<function\s*=\s*bash\s*>\s*"
    r"<parameter\s*=\s*command\s*>(.*?)</parameter\s*>"
    r"\s*</function\s*>\s*", re.DOTALL,
)
_DIALECT = re.compile(r"<\s*/?\s*(?:function|parameter)\b")


def normalize_groq_tool_text(text: str) -> str:
    """Translate complete observed calls to JSON, without decoding shell text.

    Invalid dialect blocks become empty tool markers: the existing recovery
    instruction can request a corrected call, but no inner JSON/partial call
    can accidentally be picked up by a later parser fallback.
    """
    def replace(match):
        body = match.group(1)
        if not _DIALECT.match(body.lstrip()):
            return match.group(0)
        call = _CALL.fullmatch(body)
        if (call is None or not re.search(r"</tool_call\s*>$", match.group(0))
                or _DIALECT.search(call.group(1))
                or re.search(r"</?tool_call\b", call.group(1))):
            return "<tool_call></tool_call>"
        return "<tool_call>" + json.dumps(
            {"tool": "bash", "command": call.group(1)}, ensure_ascii=False,
        ) + "</tool_call>"

    return _BLOCK.sub(replace, text)

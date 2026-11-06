"""Typed, turn-scoped slash-command contracts shared by PC chat surfaces.

The palette is a composer feature, not a prompt preprocessor.  This module
keeps the wire shape bounded and validates stable identities before a turn can
reach attachments, routing, or a provider.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
MAX_WIRE_BYTES = 4096
MAX_MODIFIERS = 4
MAX_FILTER_CHARS = 96
MAX_RESULTS = 24
MAX_RESULTS_PER_CATEGORY = 8
MAX_TITLE_CHARS = 120
MAX_PROJECT_CHARS = 80
_REASONING_OPTION_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_PARTIAL_ALIAS_RE = re.compile(r"^(?:[a-z][a-z0-9-]{0,31})?$")
_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "slash_commands"
    / "catalog.v1.json"
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "connector_hints",
        "capability_requirements",
        "conversation_refs",
        "model_override",
        "reasoning_override",
        "skill_name",
        "skill_creator",
    }
)


class SlashDirectiveError(ValueError):
    """A deterministic, localizable rejection before provider execution."""

    def __init__(self, code: str, en: str, fr: str | None = None) -> None:
        super().__init__(en)
        self.code = str(code)
        self.en = str(en)
        self.fr = str(fr or en)

    def message(self, locale: str = "en") -> str:
        return self.fr if str(locale).lower().startswith("fr") else self.en


def _object(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise SlashDirectiveError(
            "invalid_directives", f"{field_name} must be an object.",
            f"{field_name} doit être un objet.",
        )
    return raw


def _exact_keys(raw: Mapping[str, Any], allowed: Iterable[str], field_name: str) -> None:
    unknown = set(raw) - set(allowed)
    if unknown:
        raise SlashDirectiveError(
            "unknown_directive_metadata",
            f"Unknown typed metadata in {field_name}.",
            f"Métadonnée typée inconnue dans {field_name}.",
        )


def _bounded_text(value: Any, field_name: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise SlashDirectiveError(
            "invalid_directives", f"{field_name} must be text.",
            f"{field_name} doit être du texte.",
        )
    text = value.strip()
    if (required and not text) or len(text) > maximum or any(ord(ch) < 32 for ch in text):
        raise SlashDirectiveError(
            "invalid_directives", f"Invalid {field_name}.", f"{field_name} invalide.",
        )
    return text


@dataclass(frozen=True)
class ConnectorHint:
    connector_id: str

    @classmethod
    def from_wire(cls, raw: Any) -> "ConnectorHint":
        value = _object(raw, "connector_hints[]")
        _exact_keys(value, {"connector_id"}, "connector_hints[]")
        connector_id = _bounded_text(value.get("connector_id"), "connector_id", 64)
        return cls(connector_id=connector_id)

    def to_wire(self) -> dict[str, str]:
        return {"connector_id": self.connector_id}


@dataclass(frozen=True)
class CapabilityRequirements:
    current_web: bool = False

    @classmethod
    def from_wire(cls, raw: Any) -> "CapabilityRequirements":
        value = _object(raw, "capability_requirements")
        _exact_keys(value, {"current_web"}, "capability_requirements")
        current_web = value.get("current_web", False)
        if not isinstance(current_web, bool):
            raise SlashDirectiveError(
                "invalid_directives", "current_web must be boolean.",
                "current_web doit être booléen.",
            )
        return cls(current_web=current_web)

    def to_wire(self) -> dict[str, bool]:
        return {"current_web": self.current_web}


@dataclass(frozen=True)
class ConversationRef:
    conversation_id: str
    display_title: str = ""
    display_project: str = ""

    @classmethod
    def from_wire(cls, raw: Any) -> "ConversationRef":
        value = _object(raw, "conversation_refs[]")
        _exact_keys(
            value,
            {"conversation_id", "display_title", "display_project"},
            "conversation_refs[]",
        )
        return cls(
            conversation_id=_bounded_text(
                value.get("conversation_id"), "conversation_id", 128
            ),
            display_title=_bounded_text(
                value.get("display_title", ""),
                "display_title",
                MAX_TITLE_CHARS,
                required=False,
            ),
            display_project=_bounded_text(
                value.get("display_project", ""),
                "display_project",
                MAX_PROJECT_CHARS,
                required=False,
            ),
        )

    def to_wire(self) -> dict[str, str]:
        return {
            "conversation_id": self.conversation_id,
            "display_title": self.display_title,
            "display_project": self.display_project,
        }


@dataclass(frozen=True)
class ModelOverride:
    provider_id: str
    model_id: str

    @classmethod
    def from_wire(cls, raw: Any) -> "ModelOverride":
        value = _object(raw, "model_override")
        _exact_keys(value, {"provider_id", "model_id"}, "model_override")
        return cls(
            provider_id=_bounded_text(value.get("provider_id"), "provider_id", 64),
            model_id=_bounded_text(value.get("model_id"), "model_id", 192),
        )

    def to_wire(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "model_id": self.model_id}


@dataclass(frozen=True)
class SlashTurnDirectivesV1:
    schema_version: int = SCHEMA_VERSION
    connector_hints: tuple[ConnectorHint, ...] = field(default_factory=tuple)
    capability_requirements: CapabilityRequirements = field(
        default_factory=CapabilityRequirements
    )
    conversation_refs: tuple[ConversationRef, ...] = field(default_factory=tuple)
    model_override: ModelOverride | None = None
    reasoning_override: str | None = None
    skill_name: str | None = None
    skill_creator: bool = False

    @classmethod
    def from_wire(cls, raw: Any) -> "SlashTurnDirectivesV1":
        value = _object(raw, "turn_directives")
        _exact_keys(value, _TOP_LEVEL_KEYS, "turn_directives")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise SlashDirectiveError(
                "unsupported_directive_schema",
                "Unsupported slash directive schema.",
                "Schéma de directives slash non pris en charge.",
            )
        raw_connectors = value.get("connector_hints", [])
        raw_refs = value.get("conversation_refs", [])
        if not isinstance(raw_connectors, list) or not isinstance(raw_refs, list):
            raise SlashDirectiveError(
                "invalid_directives", "Directive collections must be arrays.",
                "Les collections de directives doivent être des tableaux.",
            )
        if len(raw_connectors) > 1 or len(raw_refs) > 1:
            raise SlashDirectiveError(
                "directive_limit_exceeded", "Only one selector of each kind is allowed.",
                "Un seul sélecteur de chaque type est autorisé.",
            )
        connectors = tuple(ConnectorHint.from_wire(item) for item in raw_connectors)
        refs = tuple(ConversationRef.from_wire(item) for item in raw_refs)
        requirements = CapabilityRequirements.from_wire(
            value.get("capability_requirements", {})
        )
        model = (
            ModelOverride.from_wire(value["model_override"])
            if value.get("model_override") is not None
            else None
        )
        reasoning = value.get("reasoning_override")
        if reasoning is not None:
            if not isinstance(reasoning, str) or not _REASONING_OPTION_RE.fullmatch(reasoning):
                raise SlashDirectiveError(
                    "unsupported_reasoning",
                    "Unsupported reasoning level.",
                    "Niveau de raisonnement non pris en charge.",
                )
        skill_name = value.get("skill_name")
        if skill_name is not None:
            skill_name = _bounded_text(skill_name, "skill_name", 256)
            if not skill_name.lower().endswith(".md") or any(
                char in skill_name for char in ("/", "\\")
            ):
                raise SlashDirectiveError(
                    "invalid_skill_name", "Invalid skill selection.",
                    "Sélection de skill invalide.",
                )
        skill_creator = value.get("skill_creator", False)
        if type(skill_creator) is not bool:
            raise SlashDirectiveError(
                "invalid_directives", "skill_creator must be a boolean.",
                "skill_creator doit être un booléen.",
            )
        result = cls(
            connector_hints=connectors,
            capability_requirements=requirements,
            conversation_refs=refs,
            model_override=model,
            reasoning_override=reasoning,
            skill_name=skill_name,
            skill_creator=skill_creator,
        )
        result._validate_bounds()
        return result

    def _validate_bounds(self) -> None:
        if self.skill_name and self.skill_creator:
            raise SlashDirectiveError(
                "conflicting_skill_commands",
                "Choose a skill or create one for this turn.",
                "Choisissez un skill ou créez-en un pour ce tour.",
            )
        count = (
            len(self.connector_hints)
            + len(self.conversation_refs)
            + int(self.capability_requirements.current_web)
            + int(self.model_override is not None or self.reasoning_override is not None)
            + int(bool(self.skill_name) or self.skill_creator)
        )
        if count > MAX_MODIFIERS:
            raise SlashDirectiveError(
                "directive_limit_exceeded",
                "At most four slash modifiers are allowed.",
                "Quatre modificateurs slash au maximum sont autorisés.",
            )
        if len(self.canonical_json().encode("utf-8")) > MAX_WIRE_BYTES:
            raise SlashDirectiveError(
                "directives_too_large", "Slash directives are too large.",
                "Les directives slash sont trop volumineuses.",
            )

    @property
    def is_empty(self) -> bool:
        return not (
            self.connector_hints
            or self.conversation_refs
            or self.capability_requirements.current_web
            or self.model_override
            or self.reasoning_override is not None
            or self.skill_name
            or self.skill_creator
        )

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "connector_hints": [item.to_wire() for item in self.connector_hints],
            "capability_requirements": self.capability_requirements.to_wire(),
            "conversation_refs": [item.to_wire() for item in self.conversation_refs],
        }
        if self.model_override is not None:
            out["model_override"] = self.model_override.to_wire()
        if self.reasoning_override is not None:
            out["reasoning_override"] = self.reasoning_override
        if self.skill_name is not None:
            out["skill_name"] = self.skill_name
        if self.skill_creator:
            out["skill_creator"] = True
        return out

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_wire(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def require_sendable(self, *, body: str, has_attachment: bool) -> None:
        if self.skill_creator and not str(body or "").strip():
            raise SlashDirectiveError(
                "skill_description_required", "Describe the skill you want to create.",
                "Décris le skill que tu veux créer.",
            )
        if not self.is_empty and not str(body or "").strip() and not has_attachment:
            raise SlashDirectiveError(
                "directive_body_required",
                "Add a message or attachment before sending these modifiers.",
                "Ajoutez un message ou une pièce jointe avant d’envoyer ces modificateurs.",
            )


EMPTY_DIRECTIVES = SlashTurnDirectivesV1()

_CHAT_COMMAND_CONTEXT: ContextVar[tuple[bool, SlashTurnDirectivesV1]] = ContextVar(
    "osia_chat_command_context", default=(False, EMPTY_DIRECTIVES),
)


@contextmanager
def chat_command_scope(conversation_id: str, directives: SlashTurnDirectivesV1 | None):
    """Bind the canonical conversation flag and explicit composer choices."""
    from osia.conversations import conversation_special_mode

    token = _CHAT_COMMAND_CONTEXT.set((
        conversation_special_mode(conversation_id),
        directives or EMPTY_DIRECTIVES,
    ))
    try:
        yield
    finally:
        _CHAT_COMMAND_CONTEXT.reset(token)


def current_chat_commands() -> tuple[bool, SlashTurnDirectivesV1]:
    return _CHAT_COMMAND_CONTEXT.get()


def validate_special_image_input(conversation_id: str, *, has_image: bool) -> None:
    from osia.conversations import conversation_special_mode

    if has_image and conversation_special_mode(conversation_id):
        raise SlashDirectiveError(
            "special_mode_image_disabled",
            "Use /disablevision to restore vision before sending an image.",
            "Utilise /disablevision pour réactiver la vision avant d’envoyer une image.",
        )


@dataclass(frozen=True)
class ConnectorAvailability:
    connector_id: str
    alias: str
    display_name: str
    family: str
    state: str
    configured: bool
    usable: bool
    tool_names: tuple[str, ...] = field(default_factory=tuple)

    def palette_item(self, locale: str = "en") -> dict[str, Any]:
        suffix = "Available" if self.usable else "Unavailable"
        if str(locale).lower().startswith("fr"):
            suffix = "Disponible" if self.usable else "Indisponible"
        if not self.usable:
            state = self.state
            if str(locale).lower().startswith("fr"):
                state = {
                    "disconnected": "déconnecté",
                    "failed": "échec",
                    "authorization_required": "autorisation requise",
                    "configuration_missing": "configuration manquante",
                }.get(self.state, "indisponible")
            suffix = f"{suffix} · {state}"
        return {
            "id": f"connector.{self.connector_id}",
            "canonical_alias": self.alias,
            "name": self.display_name,
            "description": suffix,
            "kind": "modifier",
            "category": "connectors",
            "enabled": self.usable,
            "icon_semantic": "connector",
            "chip_behavior": "removable",
            "handler_contract": "select_connector_hint_v1",
            "connector_id": self.connector_id,
        }


@lru_cache(maxsize=1)
def load_catalog() -> tuple[dict[str, Any], ...]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if raw.get("schema_version") != SCHEMA_VERSION or not isinstance(raw.get("entries"), list):
        raise RuntimeError("Invalid slash command catalog")
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    for item in raw["entries"]:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid slash command entry")
        identity = str(item.get("id") or "")
        alias = str(item.get("canonical_alias") or "")
        if not identity or identity in seen_ids or not ALIAS_RE.fullmatch(alias) or alias in seen_aliases:
            raise RuntimeError("Invalid slash command identity")
        if item.get("kind") not in {"modifier", "selector", "informational", "action"}:
            raise RuntimeError("Invalid slash command kind")
        seen_ids.add(identity)
        seen_aliases.add(alias)
        entries.append(dict(item))
    return tuple(entries)


def slash_palette_query(text: str) -> str | None:
    """Return a bounded initial palette filter, or ``None`` for literal text."""
    source = str(text or "")
    stripped = source.lstrip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return None
    remainder = stripped[1:].lstrip()
    token = remainder.split(maxsplit=1)[0] if remainder else ""
    if len(token) > MAX_FILTER_CHARS or not _PARTIAL_ALIAS_RE.fullmatch(token):
        return None
    return token


@dataclass(frozen=True)
class ConversationMention:
    start: int
    end: int
    query: str


def conversation_mention(text: str, cursor: int | None = None) -> ConversationMention | None:
    """Find a standalone @ query ending at the caret, including within a draft."""
    end = len(text) if cursor is None else cursor
    if end < 1 or end > len(text):
        return None
    start = text.rfind("@", max(0, end - MAX_FILTER_CHARS - 1), end)
    if start < 0 or (start > 0 and not text[start - 1].isspace()):
        return None
    query = text[start + 1:end]
    if "\n" in query or "\r" in query:
        return None
    return ConversationMention(start, end, query.strip())


def escaped_literal_text(text: str) -> str:
    source = str(text or "")
    leading = len(source) - len(source.lstrip())
    if source[leading:].startswith("//"):
        return source[:leading] + source[leading + 1 :]
    return source


def consume_selected_alias(text: str, alias: str) -> str:
    source = str(text or "")
    if not ALIAS_RE.fullmatch(str(alias or "")) or slash_palette_query(source) != alias:
        return source
    return consume_palette_prefix(source)


def consume_palette_prefix(text: str) -> str:
    """Consume the currently filtered initial slash token after a selection."""
    source = str(text or "")
    leading = len(source) - len(source.lstrip())
    query = slash_palette_query(source)
    if query is None:
        return source
    remainder = source[leading + 1:].lstrip()
    return source[:leading] + remainder[len(query):].lstrip()


def _legacy_gmail(entry: Mapping[str, Any]) -> bool:
    sid = str(entry.get("id") or "").strip().lower()
    url = str(entry.get("url") or "").strip().lower()
    return sid in {"gmail", "gmail-rest", "gmail-mcp-legacy"} or (
        "gmailmcp.googleapis.com" in url
    )


def connector_availability() -> tuple[ConnectorAvailability, ...]:
    """Return only configured connectors, with selection gated by live tools."""
    from osia.config import load_settings
    from osia.mcp_client.manager import get_manager

    settings = load_settings()
    saved = settings.get("mcp_servers") or []
    saved = saved if isinstance(saved, list) else []
    runtime = get_manager().servers()
    out: list[ConnectorAvailability] = []

    # Native Gmail owns /gmail.  A legacy Gmail MCP entry never does.
    gmail_configured = False
    try:
        from osia.integrations import google_oauth

        gmail_configured = google_oauth.oauth_client_configured("gmail-rest")
    except Exception:
        pass
    try:
        from osia.tools.gmail import gmail_connection_status

        gmail = gmail_connection_status(validate=False)
    except Exception:
        gmail = {}
    if (
        gmail_configured
        or gmail.get("connected")
        or str(gmail.get("status") or "") == "failed"
    ):
        usable = bool(gmail.get("connected"))
        state = str(gmail.get("status") or "disconnected").strip()
        out.append(
            ConnectorAvailability(
                connector_id="gmail-rest",
                alias="gmail",
                display_name="Gmail",
                family="Mail",
                state="connected" if usable else (state or "disconnected"),
                configured=True,
                usable=usable,
                tool_names=(
                    "gmail_search",
                    "gmail_read_message",
                    "gmail_read_thread",
                ),
            )
        )

    for entry in saved:
        if not isinstance(entry, Mapping) or _legacy_gmail(entry):
            continue
        server_id = str(entry.get("id") or "").strip().lower()
        if not ALIAS_RE.fullmatch(server_id):
            continue
        enabled = entry.get("enabled") is not False
        snapshot = runtime.get(server_id) if isinstance(runtime, Mapping) else None
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        state = str(snapshot.get("status") or "disconnected")
        tool_count = int(snapshot.get("tools") or 0)
        usable = enabled and state == "connected" and tool_count > 0
        try:
            tools = get_manager().list_tools(server_id) if usable else []
        except Exception:
            tools = []
        names = tuple(
            str(item.get("name") or "")
            for item in tools
            if isinstance(item, Mapping) and item.get("name")
        )
        display = _bounded_text(
            entry.get("name") or server_id,
            "connector display name",
            80,
        )
        out.append(
            ConnectorAvailability(
                connector_id=server_id,
                alias=server_id,
                display_name=display,
                family="Connector",
                state=state if enabled else "disconnected",
                configured=True,
                usable=usable,
                tool_names=names,
            )
        )
    return tuple(sorted(out, key=lambda item: (item.alias, item.connector_id)))


def model_availability(*, refresh: bool = False) -> tuple[dict[str, Any], ...]:
    from osia.mobile_model_catalogue import build_catalogue

    catalogue = build_catalogue(refresh_local=refresh, refresh_account=refresh)
    models = catalogue.get("models") if isinstance(catalogue, Mapping) else []
    out = []
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, Mapping) or item.get("selectable") is False or item.get("usable") is False:
            continue
        provider_id = str(item.get("providerId") or "").strip()
        model_id = str(item.get("id") or "").strip()
        if not provider_id or not model_id:
            continue
        out.append(dict(item))
    return tuple(out)


def conversation_availability(*, current_conversation_id: str = "") -> tuple[dict[str, str], ...]:
    from osia.conversations import list_conversations, list_folders

    folder_names = {
        str(item.get("id") or ""): str(item.get("name") or "")[:MAX_PROJECT_CHARS]
        for item in list_folders()
        if isinstance(item, Mapping)
    }
    out = []
    for item in list_conversations():
        if not isinstance(item, Mapping):
            continue
        conversation_id = str(item.get("id") or "")
        if not conversation_id or conversation_id == current_conversation_id:
            continue
        folder_id = str(item.get("folder_id") or "")
        out.append(
            {
                "conversation_id": conversation_id,
                "display_title": str(item.get("title") or "")[:MAX_TITLE_CHARS],
                "display_project": folder_names.get(folder_id, ""),
            }
        )
    return tuple(out)


def current_web_available(provider_id: str = "") -> bool:
    from osia.capability_federation import federated_capabilities

    return any(
        item.available and item.equivalence_class == "web_search"
        for item in federated_capabilities(str(provider_id or ""))
    )


def palette_items(
    *,
    locale: str = "en",
    filter_text: str = "",
    models_available: bool = False,
    web_available: bool = False,
    vision_disabled: bool = False,
    connectors: Iterable[ConnectorAvailability] = (),
) -> tuple[dict[str, Any], ...]:
    """Filter a prepared snapshot without disk, provider, or connector probes."""
    language = "fr" if str(locale).lower().startswith("fr") else "en"
    query = str(filter_text or "")[:MAX_FILTER_CHARS].strip().casefold()
    items: list[dict[str, Any]] = []
    for raw in load_catalog():
        predicate = raw["availability_predicate"]
        available = not (
            (predicate == "current_web_available" and not web_available)
            or (predicate == "selectable_models_present" and not models_available)
        )
        if not available:
            continue
        item = dict(raw)
        item["name"] = str(raw["name"].get(language) or raw["name"].get("en") or "")
        item["description"] = str(
            raw["description"].get(language) or raw["description"].get("en") or ""
        )
        if raw["id"] == "special.toggle" and vision_disabled:
            item["description"] = (
                "Réactiver la vision et la souris dans cette discussion"
                if language == "fr" else
                "Restore vision and mouse in this conversation"
            )
        item["argument_hint"] = str(
            raw["argument_hint"].get(language) or raw["argument_hint"].get("en") or ""
        )
        item["category"] = "commands"
        item["enabled"] = True
        items.append(item)
    items.extend(item.palette_item(language) for item in connectors)
    if query:
        items = [
            item
            for item in items
            if query in str(item.get("canonical_alias") or "").casefold()
            or query in str(item.get("name") or "").casefold()
            or query in str(item.get("description") or "").casefold()
        ]

        def alias_priority(item: Mapping[str, Any]) -> int:
            alias = str(item.get("canonical_alias") or "").casefold()
            return 0 if alias == query else 1 if alias.startswith(query) else 2

        items.sort(key=alias_priority)
    counts: dict[str, int] = {}
    bounded = []
    for item in items:
        category = str(item.get("category") or "commands")
        if category != "commands" and counts.get(category, 0) >= MAX_RESULTS_PER_CATEGORY:
            continue
        counts[category] = counts.get(category, 0) + 1
        bounded.append(item)
        if len(bounded) >= MAX_RESULTS:
            break
    return tuple(bounded)


def conversation_palette_items(
    conversations: Iterable[Mapping[str, str]], query: str, locale: str = "en",
) -> list[dict[str, Any]]:
    """Search existing titles and project names for the @ picker."""
    needle = query.casefold()
    rows = []
    for item in conversations:
        title = item["display_title"] or ("Sans titre" if locale == "fr" else "Untitled")
        project = item["display_project"]
        if needle and needle not in title.casefold() and needle not in project.casefold():
            continue
        rows.append({
            "selection_type": "conversation",
            "name": title,
            "description": project,
            "enabled": True,
            **item,
        })
        if len(rows) >= MAX_RESULTS:
            break
    return rows


def validate_live(
    directives: SlashTurnDirectivesV1,
    *,
    current_conversation_id: str = "",
    has_attachment: bool = False,
    requires_image_attachment: bool = False,
    refresh_models: bool = False,
) -> SlashTurnDirectivesV1:
    """Validate every identity against current canonical state, without mutation."""
    directives._validate_bounds()
    if directives.skill_name is not None:
        from osia.personality import get_skill_definition

        skill = get_skill_definition(directives.skill_name)
        if skill is None or not skill.available:
            raise SlashDirectiveError(
                "skill_unavailable", "The selected skill is unavailable.",
                "Le skill sélectionné est indisponible.",
            )
    connectors = {item.connector_id: item for item in connector_availability()}
    for hint in directives.connector_hints:
        connector = connectors.get(hint.connector_id)
        if connector is None or not connector.usable:
            raise SlashDirectiveError(
                "connector_unavailable", "The selected connector is unavailable.",
                "Le connecteur sélectionné est indisponible.",
            )
    if directives.capability_requirements.current_web:
        if directives.model_override is not None:
            provider = directives.model_override.provider_id
        else:
            from osia.config import load_settings

            provider = str(load_settings().get("selected_model_provider") or "")
        if not current_web_available(provider):
            raise SlashDirectiveError(
                "current_web_unavailable", "Current web evidence is unavailable.",
                "La preuve web actuelle est indisponible.",
            )
    selected_model = None
    if directives.model_override is not None:
        selected_model = next(
            (
                item
                for item in model_availability(refresh=refresh_models)
                if str(item.get("providerId") or "") == directives.model_override.provider_id
                and str(item.get("id") or "") == directives.model_override.model_id
            ),
            None,
        )
        if selected_model is None:
            raise SlashDirectiveError(
                "model_unavailable", "The selected exact model is unavailable.",
                "Le modèle exact sélectionné est indisponible.",
            )
        if (
            requires_image_attachment
            and "image" not in tuple(selected_model.get("inputModalities") or ())
        ):
            raise SlashDirectiveError(
                "model_attachment_incompatible",
                "The selected model cannot accept this image attachment.",
                "Le modèle sélectionné ne peut pas accepter cette image.",
            )
    if selected_model is None and directives.reasoning_override is not None:
        from osia.config import load_settings

        settings = load_settings()
        active_provider = str(settings.get("selected_model_provider") or "")
        active_model = str(settings.get("selected_model") or "")
        selected_model = next(
            (
                item for item in model_availability(refresh=refresh_models)
                if str(item.get("providerId") or "") == active_provider
                and str(item.get("id") or "") == active_model
            ),
            None,
        )
        if selected_model is None:
            raise SlashDirectiveError(
                "model_unavailable", "The active model is unavailable.",
                "Le modèle actif est indisponible.",
            )
    if directives.reasoning_override is not None and directives.reasoning_override != "auto":
        if selected_model is not None:
            reasoning = selected_model.get("reasoning")
            options = reasoning.get("options") if isinstance(reasoning, Mapping) else []
            supported = {
                str(item.get("id") or "") for item in options
                if isinstance(item, Mapping)
            }
            if directives.reasoning_override not in supported:
                raise SlashDirectiveError(
                    "unsupported_reasoning",
                    "The selected model does not support that reasoning level.",
                    "Le modèle sélectionné ne prend pas en charge ce niveau de raisonnement.",
                )
    if directives.conversation_refs:
        ref = directives.conversation_refs[0]
        if ref.conversation_id == current_conversation_id:
            raise SlashDirectiveError(
                "conversation_self_reference",
                "A conversation cannot reference itself.",
                "Une conversation ne peut pas se référencer elle-même.",
            )
        from osia.conversations import get_conversation

        if get_conversation(ref.conversation_id) is None:
            raise SlashDirectiveError(
                "conversation_not_found", "The referenced conversation no longer exists.",
                "La conversation référencée n’existe plus.",
            )
    return directives


def apply_to_agent(agent: Any, directives: SlashTurnDirectivesV1) -> None:
    """Apply validated directives to a turn-owned agent without global writes."""
    agent._slash_turn_directives = directives
    agent._disable_hierarchy_for_turn = bool(directives.model_override)
    if directives.model_override is not None:
        agent.set_model(
            directives.model_override.model_id,
            provider=directives.model_override.provider_id,
        )
    agent._reasoning_override_for_turn = directives.reasoning_override


def tool_is_current_web_evidence(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name in {
        "search_public_web", "read_public_web_page",
        "search_web", "web_search", "open_search_result", "navigate_browser",
        "read_page", "get_page_text", "inspect_browser_page", "web_state",
        "reload_browser_page", "paginate_browser",
    } or name.endswith("__search_web")


def _safe_capability_family_id(item: Any) -> str:
    equivalence = str(getattr(item, "equivalence_class", "") or "")
    if equivalence == "web_search":
        return "web_search"
    if equivalence == "local_command":
        return "local_commands"
    if equivalence == "local_file_change":
        return "local_files"
    if str(getattr(item, "source", "") or "") == "mcp":
        return "connected_connectors"
    if str(getattr(item, "side_effect_class", "") or "") == "read_only":
        return "read_only_information"
    return "governed_actions"


def safe_availability_overlay(provider_id: str = "") -> dict[str, Any]:
    connectors = connector_availability()
    from osia.capability_federation import federated_capabilities

    families = sorted({
        _safe_capability_family_id(item)
        for item in federated_capabilities(provider_id)
        if item.available
    })[:24]
    return {
        "schema_version": SCHEMA_VERSION,
        "current_web_available": current_web_available(provider_id),
        "capability_families": families,
        "connectors": [
            {
                "connector_id": item.connector_id,
                "alias": item.alias,
                "display_name": item.display_name,
                "family": item.family,
                "state": item.state,
                "usable": item.usable,
            }
            for item in connectors[:MAX_RESULTS]
        ],
    }


def informational_payload(
    command_id: str,
    *,
    locale: str = "en",
    surface: str = "PC",
    provider_id: str = "",
    provider_label: str = "",
    model_label: str = "",
    reasoning: str = "auto",
    provider_ready: bool | None = None,
    routing_mode: str = "automatic",
    transport: str = "",
    models_available: bool = False,
    vision_disabled: bool = False,
) -> dict[str, Any]:
    """Build a bounded, display-only payload; it is never a chat turn."""
    language = "fr" if str(locale).lower().startswith("fr") else "en"
    all_connectors = connector_availability()
    connectors = all_connectors[:8]
    if command_id == "help.info":
        rows = palette_items(
            locale=language,
            models_available=models_available,
            web_available=current_web_available(provider_id),
            vision_disabled=vision_disabled,
            connectors=all_connectors,
        )
        commands = [{
            "alias": "@",
            "description": "Référencer une discussion" if language == "fr" else "Reference a conversation",
            "argument_hint": "discussion" if language == "fr" else "conversation",
        }]
        commands.extend({
            "alias": "/" + str(item.get("canonical_alias") or ""),
            "description": str(item.get("description") or "")[:160],
            "argument_hint": str(item.get("argument_hint") or "")[:80],
        } for item in rows if item.get("enabled") is True)
        return {
            "title": "Aide" if language == "fr" else "Help",
            "commands": commands[:MAX_RESULTS],
        }
    if command_id != "status.info":
        raise SlashDirectiveError(
            "unknown_informational_command", "Unknown informational command.",
            "Commande d’information inconnue.",
        )
    payload = {
        "title": "État d’OSIA" if language == "fr" else "OSIA status",
        "surface": "Mobile" if str(surface).lower() == "mobile" else "PC",
        "routing_mode": (
            "exact" if str(routing_mode).lower() == "exact" else "automatic"
        ),
        "provider": str(provider_label or "Automatic")[:80],
        "model": str(model_label or "Automatic")[:120],
        "reasoning": str(reasoning or "auto")[:32],
        "vision_disabled": vision_disabled,
        "provider_ready": (
            bool(provider_id) if provider_ready is None else bool(provider_ready)
        ),
        "connector_count": len(all_connectors),
        "connector_ready_count": sum(item.usable for item in all_connectors),
        "connectors": [
            {
                "name": item.display_name[:80],
                "state": (
                    {
                        "connected": "connecté",
                        "disconnected": "déconnecté",
                        "failed": "échec",
                        "authorization_required": "autorisation requise",
                        "configuration_missing": "configuration manquante",
                    }.get(item.state, "indisponible")
                    if language == "fr" else item.state
                ),
                "family": (
                    {"Mail": "Courrier", "Connector": "Connecteur"}.get(
                        item.family, "Capacité"
                    )
                    if language == "fr" else item.family
                ),
            }
            for item in connectors
        ],
    }
    if str(surface).lower() == "mobile":
        payload["transport"] = str(transport or "offline")[:16]
    return payload

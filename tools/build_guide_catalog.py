"""Build, validate, and synchronize the semantic WOR-244 Guide catalog.

The existing PC curriculum remains the audited authoring input during the
compatibility window.  This tool performs the approved deterministic split,
applies the single factual Settings correction, and emits byte-stable JSON and
typed Mobile data.  It deliberately has no network dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.guide_content import (  # noqa: E402
    CATEGORIES,
    CATEGORY_ADVANCED,
    CATEGORY_ESSENTIAL,
    CATEGORY_MASTERY,
    LESSONS_BY_ID,
    GuideLesson,
)

SCHEMA_VERSION = 4
CONTENT_VERSION = 4
CATALOG_PATH = ROOT / "assets" / "guide" / "catalog.v4.json"
DEFAULT_MOBILE_ROOT = ROOT.parent / "osia-mobile"

Localized = dict[str, str]


def _l(en: str, fr: str) -> Localized:
    return {"en": en, "fr": fr}


ESSENTIAL_SPLITS = {
    "essential.what-is-osia": [
        ("essential.osia-role", _l("OSIA works from your PC", "OSIA fonctionne depuis votre PC")),
        ("essential.requirements", _l("What OSIA needs", "Ce dont OSIA a besoin")),
    ],
    "essential.answer-methods": [
        ("essential.ai-route", _l("Choose one AI route", "Choisir une méthode IA")),
    ],
    "essential.chatgpt-account": [
        ("essential.chatgpt-account", _l("Connect a ChatGPT Account", "Connecter un compte ChatGPT")),
    ],
    "essential.api-key": [
        ("essential.api-key", _l("Add an API key", "Ajouter une clé API")),
    ],
    "essential.mobile-pairing": [
        ("essential.pairing-prepare", _l("Prepare to pair", "Préparer l’association")),
        ("essential.pairing-token", _l("Find the pairing token", "Trouver le jeton d’association")),
        ("essential.pairing-connect", _l("Connect the phone", "Connecter le téléphone")),
        ("essential.pairing-trust", _l("Know what Connected means", "Comprendre l’état Connecté")),
    ],
    "essential.menu-tour": [
        ("essential.pc-settings-map", _l("The 11 Settings categories", "Les 11 catégories des Réglages")),
        ("essential.pc-navigation", _l("Move around OSIA PC", "Naviguer dans OSIA PC")),
        ("essential.mobile-navigation", _l("Move around OSIA Mobile", "Naviguer dans OSIA Mobile")),
    ],
    "essential.first-requests": [
        ("essential.first-request", _l("Make the first request", "Faire la première demande")),
        ("essential.review-actions", _l("Review before action", "Vérifier avant une action")),
        ("essential.voice", _l("Voice is optional", "La voix est facultative")),
    ],
    "essential.ready": [
        ("essential.reconnect", _l("Reconnect calmly", "Se reconnecter sereinement")),
        ("essential.ready", _l("You are ready", "Vous êtes prêt")),
    ],
}

MASTERY_SPLITS = {
    "mastery.mental-model": [
        ("mastery.pc-authority", _l("The PC is the authority", "Le PC fait autorité")),
    ],
    "mastery.conversations-turns": [
        ("mastery.conversation-history", _l("Conversation history", "Historique de conversation")),
        ("mastery.active-turn", _l("One active turn", "Un seul tour actif")),
    ],
    "mastery.providers-accounts": [
        ("mastery.provider-account", _l("Providers and accounts", "Fournisseurs et comptes")),
    ],
    "advanced.provider-api-hierarchy": [
        ("mastery.provider-routes", _l("Provider routes", "Routes des fournisseurs")),
        ("mastery.key-chain", _l("Key rotation comes first", "La rotation des clés d’abord")),
        ("mastery.endpoint-fallback", _l("Endpoint fallback", "Repli d’endpoint")),
    ],
    "advanced.ai-model-hierarchy": [
        ("mastery.manual-model", _l("Manual model selection", "Sélection manuelle du modèle")),
        ("mastery.task-routing", _l("Task-based routing", "Routage selon la tâche")),
        ("mastery.model-fallback", _l("Model fallback", "Repli de modèle")),
        ("mastery.vision-local", _l("Vision and local models", "Vision et modèles locaux")),
    ],
    "mastery.reasoning": [
        ("mastery.reasoning", _l("Thinking and reasoning", "Réflexion et raisonnement")),
    ],
    "mastery.keys-quotas-security": [
        ("mastery.key-security", _l("Keys, quotas and security", "Clés, quotas et sécurité")),
    ],
    "mastery.files-images": [
        ("mastery.attachments-vision", _l("Files and images", "Fichiers et images")),
    ],
    "advanced.personality-skills-automation": [
        ("mastery.personality", _l("Personality", "Personnalité")),
        ("mastery.skills", _l("Skills", "Compétences")),
        ("mastery.tools", _l("Tools", "Outils")),
        ("mastery.schedules", _l("Scheduled tasks", "Tâches planifiées")),
        ("mastery.heartbeat", _l("Heartbeat monitoring", "Surveillance Heartbeat")),
    ],
    "mastery.connectors": [
        ("mastery.connector-auth", _l("Connectors and authentication", "Connecteurs et authentification")),
        ("mastery.connector-safety", _l("Connector write safety", "Sécurité des écritures connecteur")),
    ],
    "advanced.memory-conversations": [
        ("mastery.history-vs-memory", _l("History is not memory", "L’historique n’est pas la mémoire")),
        ("mastery.memory-control", _l("Control persistent memory", "Contrôler la mémoire durable")),
        ("mastery.memory-privacy", _l("Memory and privacy", "Mémoire et confidentialité")),
    ],
    "mastery.computer-use": [
        ("mastery.control-hierarchy", _l("Computer-control hierarchy", "Hiérarchie de contrôle du PC")),
        ("mastery.verify-control", _l("Verify the result", "Vérifier le résultat")),
    ],
    "advanced.voice-tts-stt": [
        ("mastery.voice-tts", _l("TTS speaks", "La TTS parle")),
        ("mastery.voice-stt", _l("STT listens", "La STT écoute")),
        ("mastery.voice-recovery", _l("Recover from voice errors", "Corriger les erreurs vocales")),
        ("mastery.voice-privacy", _l("Voice privacy", "Confidentialité vocale")),
    ],
    "advanced.pc-mobile-connection": [
        ("mastery.mobile-pairing", _l("Pairing establishes trust", "L’association établit la confiance")),
        ("mastery.mobile-transport", _l("Direct-first transport", "Transport direct d’abord")),
        ("mastery.single-transport", _l("One committed transport", "Un seul transport engagé")),
        ("mastery.mobile-offline", _l("What offline means", "Ce que signifie hors ligne")),
    ],
    "mastery.notifications-background": [
        ("mastery.notifications", _l("Notifications and background work", "Notifications et arrière-plan")),
    ],
    "mastery.settings-tour": [
        ("mastery.settings", _l("Settings tour", "Visite des Réglages")),
    ],
    "advanced.updates-safety": [
        ("mastery.update-flow", _l("Update flow", "Parcours de mise à jour")),
        ("mastery.update-trust", _l("Trust the exact build", "Faire confiance au build exact")),
        ("mastery.secret-safety", _l("Keep secrets out of evidence", "Écarter les secrets des preuves")),
    ],
    "mastery.troubleshooting": [
        ("mastery.diagnostic-layers", _l("Diagnose one layer", "Diagnostiquer une couche")),
    ],
    "advanced.usage-examples": [
        ("mastery.workflow-pc-mobile", _l("PC and Mobile workflow", "Parcours PC et Mobile")),
        ("mastery.workflow-voice", _l("Voice workflow", "Parcours vocal")),
        ("mastery.workflow-models", _l("Model workflow", "Parcours modèles")),
        ("mastery.workflow-troubleshoot", _l("Troubleshooting workflow", "Parcours de dépannage")),
    ],
}

ADVANCED_SPLITS = {
    "advanced.takeover-workflow": [
        ("advanced.takeover-orient", _l("Takeover: orient", "Reprise : s’orienter")),
        ("advanced.takeover-verify", _l("Takeover: verify", "Reprise : vérifier")),
        ("advanced.takeover-handoff", _l("Takeover: hand off", "Reprise : transmettre")),
    ],
    "advanced.glossary": [
        ("advanced.glossary-ai", _l("Glossary: AI and tools", "Glossaire : IA et outils")),
        ("advanced.glossary-transport", _l("Glossary: transport", "Glossaire : transport")),
        ("advanced.glossary-delivery", _l("Glossary: delivery", "Glossaire : livraison")),
        ("advanced.glossary-release", _l("Glossary: release", "Glossaire : publication")),
    ],
}


def _sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Ý0-9“«])", text.strip())
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _settings_correction(value: Localized) -> Localized:
    fixed = dict(value)
    fixed["en"] = fixed["en"].replace(
        "organized into ten real categories", "organized into eleven real categories"
    ).replace(
        "Memory — review and control long-term local memory.",
        "Memory — review and control long-term local memory.\nExecution — OSIA is always full-access; only system-critical destruction is blocked.",
    ).replace(
        "Memory reviews durable facts. MCP / Connectors",
        "Memory reviews durable facts. Execution is always full-access (only system-critical destruction is blocked). MCP / Connectors",
    )
    fixed["fr"] = fixed["fr"].replace(
        "organisés en dix catégories réelles", "organisés en onze catégories réelles"
    ).replace(
        "Mémoire — consultation et contrôle de la mémoire locale durable.",
        "Mémoire — consultation et contrôle de la mémoire locale durable.\nExécution — OSIA est toujours en accès complet ; seule la destruction système critique est bloquée.",
    ).replace(
        "Mémoire examine les faits durables. MCP / Connecteurs",
        "Mémoire examine les faits durables. L’exécution est toujours en accès complet (seule la destruction système critique est bloquée). MCP / Connecteurs",
    )
    return fixed


def _partition(values: list[Localized], count: int) -> list[list[Localized]]:
    corrected_values = [_settings_correction(value) for value in values]
    if count == 1:
        return [corrected_values]
    per_language: dict[str, list[str]] = {"en": [], "fr": []}
    for corrected in corrected_values:
        for language in per_language:
            per_language[language].extend(_sentences(corrected[language]))
    buckets: dict[str, list[list[str]]] = {
        language: [[] for _ in range(count)] for language in per_language
    }
    for language, parts in per_language.items():
        for index, part in enumerate(parts):
            bucket = min(count - 1, index * count // max(1, len(parts)))
            buckets[language][bucket].append(part)
    return [[{
        "en": " ".join(buckets["en"][index]),
        "fr": " ".join(buckets["fr"][index]),
    }] for index in range(count)]


def _visual(slide_id: str, title: Localized) -> dict:
    if slide_id == "essential.pairing-token":
        return {
            "kind": "screenshot",
            "key": "pairing-token-locator",
            "alt": _l(
                "Sanitized OSIA Connection / Mobile settings with the pairing token fully masked.",
                "Réglages Connexion / Mobile OSIA assainis avec le jeton entièrement masqué.",
            ),
            "asset": "onboarding/settings-mobile-pairing.png",
        }
    if any(word in slide_id for word in ("transport", "hierarchy", "flow", "architecture", "correlation", "handshake")):
        kind = "diagram"
    elif any(word in slide_id for word in ("safety", "security", "privacy", "verify", "ready")):
        kind = "callout"
    else:
        kind = "native"
    return {
        "kind": kind,
        "key": slide_id,
        "alt": _l(
            f"OSIA visual explaining {title['en'].lower()}.",
            f"Visuel OSIA expliquant {title['fr'].lower()}.",
        ),
    }


def _slide_parts(lesson: GuideLesson, children: list[tuple[str, Localized]]) -> list[dict]:
    source = [_settings_correction(lesson.intro), *map(_settings_correction, lesson.paragraphs)]
    groups = _partition(source, len(children))
    slides: list[dict] = []
    for index, ((slide_id, title), group) in enumerate(zip(children, groups, strict=True)):
        subtitle = group[0] if group else title
        body = group[1:] if len(group) > 1 else []
        callouts: list[dict] = []
        if len(children) == 1 or index == max(0, len(children) - 2):
            callouts.append({
                "kind": "example",
                "title": _settings_correction(lesson.example_title),
                "body": _settings_correction(lesson.example),
            })
        if lesson.remember and (len(children) == 1 or index == len(children) - 1):
            callouts.append({
                "kind": "remember",
                "title": _l("Remember", "À retenir"),
                "body": _settings_correction(lesson.remember),
            })
        slides.append({
            "slide_id": slide_id,
            "track_id": lesson.category,
            "order": 0,
            "title": title,
            "subtitle": subtitle,
            "body_blocks": body,
            "callouts": callouts,
            "visual": _visual(slide_id, title),
            "action": {"type": "none", "target": ""},
            "legacy_ids": [lesson.id] if lesson.id != slide_id else [],
        })
    return slides


def _mobile_variant(
    subtitle: Localized,
    body: list[Localized],
    *,
    example: Localized | None = None,
    remember: Localized | None = None,
    visual: dict | None = None,
) -> dict:
    callouts = []
    if example:
        callouts.append({"kind": "example", "title": _l("Try this", "Essayez ceci"), "body": example})
    if remember:
        callouts.append({"kind": "remember", "title": _l("Remember", "À retenir"), "body": remember})
    result: dict = {"subtitle": subtitle, "body_blocks": body, "callouts": callouts}
    if visual:
        result["visual"] = visual
    return result


MOBILE_ESSENTIAL_VARIANTS = {
    "essential.osia-role": _mobile_variant(
        _l("OSIA Mobile is the phone interface for OSIA running on your computer.", "OSIA Mobile est l’interface téléphonique d’OSIA exécuté sur votre ordinateur."),
        [_l("The PC provides the intelligence and services. The phone continues conversations, sends remote requests and uses voice while that PC is running and connected.", "Le PC fournit l’intelligence et les services. Le téléphone poursuit les conversations, envoie des demandes à distance et utilise la voix tant que ce PC fonctionne et reste connecté.")],
        example=_l("Continue on the phone the planning task you started on the PC.", "Poursuivez sur le téléphone la planification commencée sur le PC."),
        remember=_l("The phone is a secure interface to your OSIA PC, not a separate AI system.", "Le téléphone est une interface sécurisée vers votre PC OSIA, pas un système IA distinct."),
    ),
    "essential.requirements": _mobile_variant(
        _l("Have OSIA running on the PC and OSIA Mobile installed on the phone.", "OSIA doit fonctionner sur le PC et OSIA Mobile doit être installé sur le téléphone."),
        [_l("Keep the PC awake and give the phone working network access. You do not need to understand ports, certificates, WebSockets, STUN, ICE or TURN.", "Laissez le PC actif et donnez au téléphone un accès réseau fonctionnel. Vous n’avez pas besoin de comprendre ports, certificats, WebSockets, STUN, ICE ou TURN.")],
        remember=_l("Treat the temporary pairing token like a password.", "Traitez le jeton d’association temporaire comme un mot de passe."),
    ),
    "essential.pairing-prepare": _mobile_variant(
        _l("The PC must be awake and reachable before pairing starts.", "Le PC doit être actif et joignable avant l’association."),
        [_l("Open OSIA on both devices and confirm that the phone has network access.", "Ouvrez OSIA sur les deux appareils et vérifiez l’accès réseau du téléphone.")],
    ),
    "essential.pairing-token": _mobile_variant(
        _l("On the PC, open Settings → Connection / Mobile and reveal the private temporary token.", "Sur le PC, ouvrez Réglages → Connexion / Mobile et affichez le jeton temporaire privé."),
        [_l("Enter or copy it only into OSIA Mobile. Tutorials and support images must use DEMO-CODE or a fully masked value.", "Saisissez-le ou copiez-le uniquement dans OSIA Mobile. Les guides et captures d’assistance doivent utiliser DEMO-CODE ou une valeur entièrement masquée.")],
        remember=_l("Never share the token or a screenshot that reveals it.", "Ne partagez jamais le jeton ni une capture qui le révèle."),
        visual={
            "kind": "screenshot", "key": "pairing-token-locator",
            "asset": "assets/guide/pc-pairing.png",
            "alt": _l("OSIA PC mobile-pairing settings with a completely masked token.", "Réglages d’association mobile OSIA PC avec un jeton entièrement masqué."),
        },
    ),
    "essential.pairing-connect": _mobile_variant(
        _l("Enter or paste the PC token on the Mobile Connection page, then validate it.", "Saisissez ou collez le jeton PC sur la page Connexion Mobile, puis validez-le."),
        [_l("Wait while the phone establishes trust. If it fails, check for an expired or mistyped token, an offline PC or missing phone network access.", "Attendez que le téléphone établisse la confiance. En cas d’échec, vérifiez le jeton expiré ou mal saisi, le PC hors ligne ou l’accès réseau du téléphone.")],
    ),
    "essential.pairing-trust": _mobile_variant(
        _l("Connected means the phone is paired and ready to exchange messages with the PC.", "Connecté signifie que le téléphone est associé et prêt à échanger avec le PC."),
        [_l("Stored trust enables later reconnects. Reinstalling the app, clearing storage or resetting PC trust can require a new token.", "La confiance enregistrée permet les reconnexions. Réinstaller l’app, effacer son stockage ou réinitialiser la confiance PC peut exiger un nouveau jeton.")],
    ),
    "essential.mobile-navigation": _mobile_variant(
        _l("Chat is the main place for active conversations and messages.", "Discussion est le lieu principal des conversations et messages actifs."),
        [_l("Chat continues the same conversation across PC and phone; Conversations manages chats; Settings holds notifications and preferences; Memory shows saved memories; Models and OSIA configure providers, skills and personality; Connection handles pairing and recovery.", "Chat poursuit la même conversation sur PC et téléphone ; Conversations gère les discussions ; Réglages contient les notifications et préférences ; Mémoire affiche les souvenirs enregistrés ; Modèles et OSIA configurent les fournisseurs, compétences et la personnalité ; Connexion gère l’association et la récupération.")],
        remember=_l("Conversations remain PC-authoritative when viewed from the phone.", "Les conversations restent gérées par le PC lorsqu’elles sont consultées sur le téléphone."),
    ),
    "essential.first-request": _mobile_variant(
        _l("Open or create a conversation, state a clear goal, add useful context and describe the result you want.", "Ouvrez ou créez une conversation, indiquez un objectif clair, ajoutez le contexte utile et décrivez le résultat souhaité."),
        [_l("Send it, wait for OSIA’s answer, then continue in the same conversation.", "Envoyez-la, attendez la réponse d’OSIA, puis continuez dans la même conversation.")],
        example=_l("Help me plan tomorrow. Give me a short, prioritized checklist.", "Aide-moi à organiser demain. Donne-moi une courte liste de priorités."),
    ),
    "essential.voice": _mobile_variant(
        _l("The microphone records speech that the PC converts into message text.", "Le microphone enregistre la parole que le PC convertit en texte de message."),
        [_l("Allow microphone access, speak, then review and send the transcript. TTS can read responses when enabled; there is no Mobile wake word.", "Autorisez le microphone, parlez, puis vérifiez et envoyez la transcription. La TTS peut lire les réponses lorsqu’elle est activée ; Mobile n’a pas de mot d’activation.")],
        remember=_l("Speech-to-text creates a message; it does not send one silently.", "La reconnaissance vocale crée un message ; elle ne l’envoie pas silencieusement."),
    ),
    "essential.reconnect": _mobile_variant(
        _l("Locking the phone or changing networks can briefly disconnect it.", "Verrouiller le téléphone ou changer de réseau peut le déconnecter brièvement."),
        [_l("Android can optionally keep a visible foreground connection; iOS may suspend the app. Notifications are local socket events, not APNs push while fully suspended. The PC must remain available.", "Android peut conserver une connexion visible au premier plan ; iOS peut suspendre l’app. Les notifications sont des événements socket locaux, pas des push APNs en suspension complète. Le PC doit rester disponible.")],
        example=_l("Reopen the app and allow one clean reconnect before sending again.", "Rouvrez l’app et laissez une reconnexion propre aboutir avant de renvoyer."),
    ),
    "essential.ready": _mobile_variant(
        _l("The PC is running, the phone is paired and the connection state is visible.", "Le PC fonctionne, le téléphone est associé et l’état de connexion est visible."),
        [_l("Messages can be sent, voice stays optional and preferences can change later.", "Les messages peuvent être envoyés, la voix reste facultative et les préférences peuvent changer plus tard.")],
        remember=_l("Finish closes this guide; Help keeps all three tracks available.", "Terminer ferme ce guide ; Aide conserve les trois parcours disponibles."),
    ),
}


def _track_slides(category_id: str, split_map: dict) -> list[dict]:
    slides: list[dict] = []
    lessons = sorted(
        (lesson for lesson in LESSONS_BY_ID.values() if lesson.category == category_id),
        key=lambda lesson: lesson.order,
    )
    for lesson in lessons:
        children = split_map.get(lesson.id)
        if children is None:
            children = [(lesson.id, _settings_correction(lesson.title))]
        slides.extend(_slide_parts(lesson, children))
    for order, slide in enumerate(slides, 1):
        slide["order"] = order
        if category_id == CATEGORY_ESSENTIAL and slide["slide_id"] in MOBILE_ESSENTIAL_VARIANTS:
            slide["platform_variants"] = {
                "mobile": MOBILE_ESSENTIAL_VARIANTS[slide["slide_id"]]
            }
    return slides


def build_catalog() -> dict:
    maps = {
        CATEGORY_ESSENTIAL: ESSENTIAL_SPLITS,
        CATEGORY_MASTERY: MASTERY_SPLITS,
        CATEGORY_ADVANCED: ADVANCED_SPLITS,
    }
    tracks = []
    for category in sorted(CATEGORIES, key=lambda item: item.order):
        tracks.append({
            "track_id": category.id,
            "semantic_id": category.semantic_id,
            "order": category.order,
            "title": _settings_correction(category.title),
            "summary": _settings_correction(category.summary),
            "slides": _track_slides(category.id, maps[category.id]),
        })
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "content_version": CONTENT_VERSION,
        "tracks": tracks,
    }
    validate_catalog(catalog)
    return catalog


def resolve_platform(catalog: dict, platform: str) -> dict:
    resolved = deepcopy(catalog)
    for track in resolved["tracks"]:
        for slide in track["slides"]:
            variant = slide.pop("platform_variants", {}).get(platform, {})
            slide.update(variant)
    return resolved


def validate_catalog(catalog: dict) -> None:
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("guide_schema_version")
    tracks = catalog.get("tracks")
    if not isinstance(tracks, list) or [t.get("semantic_id") for t in tracks] != ["essential", "mastery", "advanced"]:
        raise ValueError("guide_tracks")
    ids: set[str] = set()
    for expected_track_order, track in enumerate(tracks, 1):
        if track.get("order") != expected_track_order:
            raise ValueError("guide_track_order")
        slides = track.get("slides")
        if not isinstance(slides, list) or not slides:
            raise ValueError("guide_slides")
        for expected_slide_order, slide in enumerate(slides, 1):
            slide_id = slide.get("slide_id")
            if not isinstance(slide_id, str) or slide_id in ids:
                raise ValueError("guide_slide_id")
            ids.add(slide_id)
            if slide.get("track_id") != track["track_id"] or slide.get("order") != expected_slide_order:
                raise ValueError("guide_slide_order")
            for field in ("title", "subtitle"):
                value = slide.get(field)
                if not isinstance(value, dict) or not all(isinstance(value.get(lang), str) and value[lang].strip() for lang in ("en", "fr")):
                    raise ValueError(f"guide_localized_{field}")
            visual = slide.get("visual")
            if not isinstance(visual, dict) or not all(visual.get(key) for key in ("kind", "key", "alt")):
                raise ValueError("guide_visual")
            alt = visual["alt"]
            if not all(isinstance(alt.get(lang), str) and alt[lang].strip() for lang in ("en", "fr")):
                raise ValueError("guide_visual_alt")
            for alias in slide.get("legacy_ids", []):
                if not isinstance(alias, str) or not alias:
                    raise ValueError("guide_alias")
    counts = [len(track["slides"]) for track in tracks]
    if counts != [17, 45, 43]:
        raise ValueError(f"guide_slide_counts:{counts}")


def _render_json(catalog: dict) -> bytes:
    return (json.dumps(catalog, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _dart_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _dart_text(value: Localized) -> str:
    return f"GuideText(en: {_dart_string(value['en'])}, fr: {_dart_string(value['fr'])})"


def _render_dart(catalog: dict) -> bytes:
    mobile = resolve_platform(catalog, "mobile")
    digest = hashlib.sha256(_render_json(catalog)).hexdigest()
    lines = [
        "// GENERATED by PC tools/build_guide_catalog.py. Do not edit.",
        "import 'guide_model.dart';",
        "",
        f"const String guideCatalogSha256 = {_dart_string(digest)};",
        "",
        "const GuideCatalog guideCatalog = GuideCatalog(",
        f"  schemaVersion: {mobile['schema_version']},",
        f"  contentVersion: {mobile['content_version']},",
        "  tracks: <GuideTrack>[",
    ]
    for track in mobile["tracks"]:
        lines.extend([
            "    GuideTrack(",
            f"      id: {_dart_string(track['track_id'])},",
            f"      semanticId: {_dart_string(track['semantic_id'])},",
            f"      order: {track['order']},",
            f"      title: {_dart_text(track['title'])},",
            f"      summary: {_dart_text(track['summary'])},",
            "      slides: <GuideSlide>[",
        ])
        for slide in track["slides"]:
            visual = slide["visual"]
            lines.extend([
                "        GuideSlide(",
                f"          id: {_dart_string(slide['slide_id'])},",
                f"          trackId: {_dart_string(slide['track_id'])},",
                f"          order: {slide['order']},",
                f"          title: {_dart_text(slide['title'])},",
                f"          subtitle: {_dart_text(slide['subtitle'])},",
                "          bodyBlocks: <GuideText>[",
            ])
            lines.extend(f"            {_dart_text(item)}," for item in slide["body_blocks"])
            lines.extend(["          ],", "          callouts: <GuideCallout>["])
            for callout in slide["callouts"]:
                lines.extend([
                    "            GuideCallout(",
                    f"              kind: {_dart_string(callout['kind'])},",
                    f"              title: {_dart_text(callout['title'])},",
                    f"              body: {_dart_text(callout['body'])},",
                    "            ),",
                ])
            lines.extend([
                "          ],",
                "          visual: GuideVisual(",
                f"            kind: {_dart_string(visual['kind'])},",
                f"            key: {_dart_string(visual['key'])},",
                f"            alt: {_dart_text(visual['alt'])},",
                f"            asset: {_dart_string(visual['asset']) if visual.get('asset') else 'null'},",
                "          ),",
                "          legacyIds: <String>[",
            ])
            lines.extend(f"            {_dart_string(value)}," for value in slide["legacy_ids"])
            lines.extend(["          ],", "        ),"])
        lines.extend(["      ],", "    ),"])
    lines.extend(["  ],", ");", ""])
    return "\n".join(lines).encode("utf-8")


def _write_if_changed(path: Path, payload: bytes) -> bool:
    if path.is_file() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--sync-mobile", type=Path, nargs="?", const=DEFAULT_MOBILE_ROOT)
    args = parser.parse_args()
    catalog = build_catalog()
    rendered = _render_json(catalog)
    if args.check:
        if not CATALOG_PATH.is_file() or CATALOG_PATH.read_bytes() != rendered:
            raise SystemExit("guide_catalog_out_of_date")
    else:
        _write_if_changed(CATALOG_PATH, rendered)
    if args.sync_mobile:
        mobile_root = args.sync_mobile.resolve()
        mobile_catalog = mobile_root / "assets" / "guide" / "catalog.v4.json"
        mobile_dart = mobile_root / "lib" / "guide" / "guide_catalog_data.g.dart"
        if args.check:
            if not mobile_catalog.is_file() or mobile_catalog.read_bytes() != rendered:
                raise SystemExit("mobile_guide_catalog_out_of_date")
            expected_dart = _render_dart(catalog)
            if not mobile_dart.is_file() or mobile_dart.read_bytes() != expected_dart:
                raise SystemExit("mobile_guide_dart_out_of_date")
        else:
            _write_if_changed(mobile_catalog, rendered)
            _write_if_changed(mobile_dart, _render_dart(catalog))
    digest = hashlib.sha256(rendered).hexdigest()
    counts = ",".join(str(len(track["slides"])) for track in catalog["tracks"])
    print(f"guide_catalog_sha256={digest} counts={counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

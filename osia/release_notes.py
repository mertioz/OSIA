"""Embedded, localized release history for the desktop application.

Remote updater notes describe a newer available build.  This module remains
the offline source of truth for versions already shipped with OSIA, so the
installed version's notes survive cache cleanup and network outages.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from osia.version import parse_product_version


@dataclass(frozen=True)
class ReleaseSection:
    title_en: str
    title_fr: str
    changes_en: tuple[str, ...]
    changes_fr: tuple[str, ...]

    def title(self, language: str) -> str:
        return self.title_fr if language == "fr" else self.title_en

    def changes(self, language: str) -> tuple[str, ...]:
        return self.changes_fr if language == "fr" else self.changes_en


@dataclass(frozen=True)
class ReleaseEntry:
    version: str
    released_on: str | None
    public: bool
    changes_en: tuple[str, ...]
    changes_fr: tuple[str, ...]
    summary_en: tuple[str, ...] = ()
    summary_fr: tuple[str, ...] = ()
    sections: tuple[ReleaseSection, ...] = ()

    def changes(self, language: str) -> tuple[str, ...]:
        if self.sections:
            return tuple(
                change
                for section in self.sections
                for change in section.changes(language)
            )
        return self.changes_fr if language == "fr" else self.changes_en

    def summary(self, language: str) -> tuple[str, ...]:
        localized = self.summary_fr if language == "fr" else self.summary_en
        return localized or self.changes(language)

    def note_sections(self, language: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        if self.sections:
            return tuple(
                (section.title(language), section.changes(language))
                for section in self.sections
            )
        return (("", self.changes(language)),)


_RELEASES = (
    ReleaseEntry(
        version="1.1.0",
        released_on="2026-08-02",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Major update: safer conversations, canonical PC/mobile history, and clearer typed diagnostics for stalled or failed work.",
            "Granular permissions, scoped delegation, deletion barriers, and revocable Full Access now cover chat, Autonomous, and Scheduled Tasks.",
            "Browser, document, PDF, image, audio, and video workflows gain deterministic navigation, structured analysis, private artifacts, and explicit limits.",
            "Voice, language continuity, provider reasoning, ChatGPT Account actions, personality, skills, packaging, and Android reliability are substantially strengthened.",
            "Windows 1.1.0 adds a dedicated major-update journey; Android 1.0.2 adds QR pairing, canonical sync, and PC-confirmed Full Access activation.",
        ),
        summary_fr=(
            "Mise à jour majeure : conversations plus sûres, historique PC/Mobile canonique et diagnostics typés plus clairs en cas de blocage ou d’échec.",
            "Les autorisations granulaires, la délégation limitée, les barrières de suppression et le Full Access révocable couvrent Chat, Autonomous et les tâches planifiées.",
            "Le navigateur, les documents, PDF, images, contenus audio et vidéo gagnent navigation déterministe, analyse structurée, artefacts privés et limites explicites.",
            "Voix, continuité de langue, raisonnement des fournisseurs, actions Compte ChatGPT, personnalité, skills, packaging et fiabilité Android sont renforcés.",
            "Windows 1.1.0 reçoit un parcours de mise à jour majeure ; Android 1.0.2 ajoute l’association QR, la synchro canonique et l’activation Full Access confirmée par le PC.",
        ),
        sections=(
            ReleaseSection(
                "Core chat and conversation reliability",
                "Chat et fiabilité des conversations",
                (
                    "Stalled turns now stop with an explicit typed diagnostic while preserving genuine progress instead of cutting a productive response short.",
                    "Autonomous and tool failures retain their concrete failure category and evidence instead of collapsing into an empty or generic final answer.",
                    "Streaming produces one authoritative final response, preventing fragmented or contradictory terminal messages across PC and Mobile.",
                    "Conversation context is selected and compacted consistently across providers so relevant earlier turns survive long sessions.",
                    "Document completion no longer falls back to an unexplained outcome_unknown state when a contextual result can be reported.",
                ),
                (
                    "Les tours bloqués s’arrêtent avec un diagnostic typé explicite tout en préservant les progrès réels, sans couper une réponse productive.",
                    "Les échecs Autonomous et d’outils conservent leur catégorie et leurs preuves concrètes au lieu de devenir une réponse finale vide ou générique.",
                    "Le streaming produit une seule réponse finale autoritaire, sans messages terminaux fragmentés ou contradictoires entre PC et Mobile.",
                    "Le contexte de conversation est sélectionné et compacté de manière cohérente entre fournisseurs afin de préserver les tours antérieurs pertinents.",
                    "La clôture d’un document ne retombe plus sur un état outcome_unknown inexpliqué lorsqu’un résultat contextualisé peut être fourni.",
                ),
            ),
            ReleaseSection(
                "PC/Mobile synchronization and canonical history",
                "Synchronisation PC/Mobile et historique canonique",
                (
                    "Conversation persistence is serialized before broadcast, eliminating races between disk history, the PC view, and connected phones.",
                    "Mobile reconciles canonical history revisions and replaces provisional local messages without duplicating completed turns.",
                    "Reconnect restores the authoritative conversation ledger and correlated read-aloud state instead of replaying stale local projections.",
                    "Provider and model identity remain attached to Autonomous runs when state crosses the PC/Mobile boundary.",
                ),
                (
                    "La persistance des conversations est sérialisée avant diffusion, supprimant les courses entre l’historique disque, le PC et les téléphones connectés.",
                    "Mobile réconcilie les révisions de l’historique canonique et remplace les messages locaux provisoires sans dupliquer les tours terminés.",
                    "La reconnexion restaure le registre de conversation autoritaire et l’état de lecture vocale corrélé au lieu de rejouer des projections locales périmées.",
                    "L’identité du fournisseur et du modèle reste attachée aux exécutions Autonomous lors des échanges entre PC et Mobile.",
                ),
            ),
            ReleaseSection(
                "Permissions, delegation, deletion, and Full Access",
                "Autorisations, délégation, suppression et Full Access",
                (
                    "A central permission store now governs granular capabilities, durable grants, one-use execution permits, expiry, revocation, and audit-safe receipts.",
                    "Legacy confirmation toggles are confined so they cannot silently bypass the canonical permission policy.",
                    "Destructive operations use explicit deletion plans and universal confirmation barriers, including shell, MCP, system-resource, and mobile paths.",
                    "Full Access is explicit, revocable, and stabilized across restarts; Default Access remains the safe fallback whenever state is missing or invalid.",
                ),
                (
                    "Un registre central gouverne désormais les capacités granulaires, droits durables, permis d’exécution uniques, expirations, révocations et reçus auditables.",
                    "Les anciens interrupteurs de confirmation sont confinés afin qu’ils ne puissent pas contourner silencieusement la politique canonique.",
                    "Les opérations destructrices utilisent des plans de suppression explicites et des barrières de confirmation universelles, y compris via shell, MCP, ressources système et Mobile.",
                    "Full Access est explicite, révocable et stabilisé après redémarrage ; Default Access reste le repli sûr si l’état manque ou devient invalide.",
                ),
            ),
            ReleaseSection(
                "Mobile permissions and remote Full Access activation",
                "Autorisations Mobile et activation distante de Full Access",
                (
                    "Android has a dedicated permissions screen instead of mixing PC-controlled access with unrelated settings.",
                    "The phone displays the PC-canonical permission snapshot and receives grant, revocation, expiry, and Full Access changes consistently.",
                    "A Full Access request from Mobile is activated only after the explicit OSIA confirmation flow on the PC, without a biometric or device-code prompt.",
                    "Revocation is synchronized back to the phone and the validated release flow finishes in the safe Default state.",
                ),
                (
                    "Android dispose d’un écran d’autorisations dédié au lieu de mélanger l’accès contrôlé par le PC avec des réglages sans rapport.",
                    "Le téléphone affiche l’instantané d’autorisations canonique du PC et reçoit de façon cohérente droits, révocations, expirations et changements Full Access.",
                    "Une demande Full Access depuis Mobile n’est activée qu’après confirmation explicite dans OSIA sur le PC, sans biométrie ni code de l’appareil.",
                    "La révocation est resynchronisée vers le téléphone et le parcours de release validé se termine dans l’état sûr Default.",
                ),
            ),
            ReleaseSection(
                "Autonomous",
                "Autonomous",
                (
                    "The Autonomous model picker shows only providers and models actually configured on the PC, including compatible ChatGPT Account models.",
                    "Plans and runs preserve typed failure diagnostics, provider identity, progress, resumable state, and safe document artifacts.",
                    "Scoped authorization limits what each run may do and keeps destructive or out-of-scope work behind the canonical permission boundary.",
                    "Resource checks and progress-aware loop detection stop unsafe or genuinely stalled work without suppressing legitimate long-running progress.",
                ),
                (
                    "Le sélecteur de modèles Autonomous n’affiche que les fournisseurs et modèles réellement configurés sur le PC, dont les modèles compatibles Compte ChatGPT.",
                    "Plans et exécutions conservent diagnostics typés, identité du fournisseur, progression, état reprenable et artefacts documentaires sûrs.",
                    "L’autorisation limitée encadre les actions de chaque exécution et maintient les travaux destructeurs ou hors périmètre derrière la politique canonique.",
                    "Les contrôles de ressources et la détection de boucle sensible à la progression arrêtent les travaux dangereux ou réellement bloqués sans masquer les progrès légitimes.",
                ),
            ),
            ReleaseSection(
                "Scheduled Tasks",
                "Tâches planifiées",
                (
                    "Scheduled Tasks receive their own scoped delegation contract rather than inheriting unlimited interactive-chat authority.",
                    "Background actions validate grants and execution permits at run time, including expiry and revocation after a task was created.",
                    "Deletion and system-resource actions remain protected when launched from scheduled or unattended contexts.",
                ),
                (
                    "Les tâches planifiées disposent de leur propre contrat de délégation limitée au lieu d’hériter d’une autorité illimitée du chat interactif.",
                    "Les actions en arrière-plan valident droits et permis au moment de l’exécution, y compris les expirations ou révocations postérieures à la création.",
                    "Les suppressions et actions sur les ressources système restent protégées dans les contextes planifiés ou sans surveillance.",
                ),
            ),
            ReleaseSection(
                "Tool execution, receipts, anti-replay, and diagnostics",
                "Exécution des outils, reçus, anti-rejeu et diagnostics",
                (
                    "Tool schemas, side-effect classes, confirmation requirements, timeouts, and smoke coverage are centralized in one safety contract.",
                    "Execution permits and correlated receipts prevent reuse across another turn, scope, target, or expired authorization.",
                    "Native Codex actions are routed through OSIA’s controlled action bridge instead of bypassing OSIA policy.",
                    "End-to-end validation now catches argument mismatches and reports contextual read or execution outcomes rather than a generic unknown result.",
                    "The advertised render_pdf timeout now matches the effective worker timeout and its cancellation behavior.",
                ),
                (
                    "Schémas d’outils, classes d’effets de bord, confirmations, délais et couverture smoke sont centralisés dans un contrat de sécurité unique.",
                    "Les permis d’exécution et reçus corrélés empêchent le rejeu dans un autre tour, périmètre, cible ou après expiration de l’autorisation.",
                    "Les actions natives Codex passent par le pont d’actions contrôlé d’OSIA au lieu de contourner sa politique.",
                    "La validation de bout en bout détecte les incohérences d’arguments et produit des résultats de lecture ou d’exécution contextualisés plutôt qu’un inconnu générique.",
                    "Le délai annoncé de render_pdf correspond désormais au délai effectif du worker et à son comportement d’annulation.",
                ),
            ),
            ReleaseSection(
                "Browser navigation and controlled web actions",
                "Navigation web et actions contrôlées",
                (
                    "Navigation is deterministic across links, redirects, history, tabs, downloads, and page-state transitions.",
                    "Forms are inspected semantically so labels, fields, buttons, and validation state can be understood before a controlled fill or submit action.",
                    "Sensitive form values stay behind privacy and confirmation boundaries and are not copied into public diagnostics or artifacts.",
                    "PC and Mobile receive correlated browser progress and completion evidence instead of ambiguous navigation status.",
                ),
                (
                    "La navigation est déterministe pour les liens, redirections, historique, onglets, téléchargements et transitions d’état de page.",
                    "Les formulaires sont inspectés sémantiquement afin de comprendre libellés, champs, boutons et validation avant un remplissage ou envoi contrôlé.",
                    "Les valeurs sensibles restent derrière les frontières de confidentialité et de confirmation, sans copie dans les diagnostics ou artefacts publics.",
                    "PC et Mobile reçoivent une progression et des preuves de fin corrélées plutôt qu’un état de navigation ambigu.",
                ),
            ),
            ReleaseSection(
                "Files, documents, Office, and OpenDocument formats",
                "Fichiers, documents, Office et OpenDocument",
                (
                    "Structured analysis covers DOCX, ODT, RTF, plain text, Markdown, safe HTML, EPUB, XLSX, ODS, CSV, TSV, PPTX, and ODP.",
                    "Word-processing structure, spreadsheet sheets and cells, presentation slides and notes, comments, tables, and pagination can be queried without loading the whole document into one prompt.",
                    "Content identity is verified from bytes and container structure, so a misleading extension or MIME declaration does not select an unsafe parser.",
                    "Archive traversal, duplicate paths, decompression bombs, external references, active content, malformed XML, and unsupported legacy binaries receive explicit typed refusals.",
                ),
                (
                    "L’analyse structurée couvre DOCX, ODT, RTF, texte brut, Markdown, HTML sûr, EPUB, XLSX, ODS, CSV, TSV, PPTX et ODP.",
                    "Structure de texte, feuilles et cellules, diapositives et notes, commentaires, tableaux et pagination sont interrogeables sans charger tout le document dans un seul prompt.",
                    "L’identité est vérifiée à partir des octets et de la structure du conteneur ; une extension ou un MIME trompeur ne choisit donc pas un parseur dangereux.",
                    "Traversée d’archive, chemins dupliqués, bombes de décompression, références externes, contenu actif, XML malformé et anciens binaires non pris en charge reçoivent des refus typés.",
                ),
            ),
            ReleaseSection(
                "Private artifacts and PDF handling",
                "Artefacts privés et gestion des PDF",
                (
                    "Large document results are retained as bounded private artifacts with separate public and private metadata instead of leaking source paths into chat.",
                    "Autonomous can resume and query those artifacts with stable identifiers, cursors, truncation markers, and explicit unsupported-query results.",
                    "PDF reading rejects encrypted, active, malformed, or resource-exhausting inputs with typed errors.",
                    "PDF rendering runs in an isolated worker with bounded inputs, outputs, time, and sanitized filenames.",
                ),
                (
                    "Les résultats documentaires volumineux sont conservés comme artefacts privés bornés, avec métadonnées publiques et privées séparées, sans fuite du chemin source dans le chat.",
                    "Autonomous peut reprendre et interroger ces artefacts avec identifiants stables, curseurs, marqueurs de troncature et résultats explicites pour les requêtes non prises en charge.",
                    "La lecture PDF refuse les entrées chiffrées, actives, malformées ou épuisant les ressources avec des erreurs typées.",
                    "Le rendu PDF s’exécute dans un worker isolé avec entrées, sorties, durée et noms de fichiers bornés et assainis.",
                ),
            ),
            ReleaseSection(
                "Images, OCR, and screenshots",
                "Images, OCR et captures d’écran",
                (
                    "Image identity and safe decoding cover PNG, JPEG, WebP, GIF, TIFF, and BMP while rejecting corrupt, encrypted, or mismatched media.",
                    "OCR output can be retained as a private resumable artifact for Autonomous instead of being lost after one turn.",
                    "Orientation, dimensions, frame limits, and public/private metadata are normalized before image analysis.",
                    "Desktop screenshots and Mobile image attachments use the same bounded image-input and privacy pipeline before provider routing.",
                ),
                (
                    "L’identité et le décodage sûr couvrent PNG, JPEG, WebP, GIF, TIFF et BMP tout en refusant les médias corrompus, chiffrés ou incohérents.",
                    "La sortie OCR peut être conservée comme artefact privé reprenable pour Autonomous au lieu d’être perdue après un seul tour.",
                    "Orientation, dimensions, limites d’images et métadonnées publiques/privées sont normalisées avant l’analyse.",
                    "Captures d’écran PC et images jointes depuis Mobile utilisent le même pipeline borné de confidentialité et d’entrée image avant routage fournisseur.",
                ),
            ),
            ReleaseSection(
                "Audio and video analysis",
                "Analyse audio et vidéo",
                (
                    "Validated media analysis supports WAV and MP3 audio plus MP4, MOV, WebM, and MKV video when their codecs can be decoded safely.",
                    "Audio tracks are selected and normalized for bounded transcription; video analysis samples bounded frames and correlates them with media metadata.",
                    "Unsupported codecs, corruption, encryption, DRM, duration, frame, and resource limits produce explicit categories instead of crashes or fabricated analysis.",
                    "Mobile media attachments preserve type, bytes, correlation, and privacy metadata through the PC analysis path.",
                ),
                (
                    "L’analyse validée prend en charge l’audio WAV et MP3 ainsi que les vidéos MP4, MOV, WebM et MKV lorsque leurs codecs sont décodables en sécurité.",
                    "Les pistes audio sont sélectionnées et normalisées pour une transcription bornée ; l’analyse vidéo échantillonne des images limitées et les corrèle aux métadonnées.",
                    "Codec non pris en charge, corruption, chiffrement, DRM et limites de durée, d’images ou de ressources produisent des catégories explicites plutôt qu’un crash ou une analyse inventée.",
                    "Les médias joints depuis Mobile préservent type, octets, corrélation et métadonnées de confidentialité jusqu’au parcours d’analyse PC.",
                ),
            ),
            ReleaseSection(
                "Voice, wake word, STT, and TTS",
                "Voix, mot de réveil, STT et TTS",
                (
                    "Wake-word gating is stricter against activations that do not contain OSIA while keeping the existing local acoustic verifier.",
                    "Manual voice dialogue has clearer capture, VAD endpointing, transcript-quality, cancellation, and UI lifecycle behavior.",
                    "Temporary recordings are removed after use on PC and Mobile, including interruption and error paths.",
                    "TTS shutdown and playback correlation are hardened so completed responses are not dropped or spoken twice.",
                    "The voice shortcut and contrast were adjusted for clearer everyday access.",
                ),
                (
                    "Le filtrage du mot de réveil est plus strict contre les activations ne contenant pas OSIA, tout en conservant le vérificateur acoustique local existant.",
                    "Le dialogue vocal manuel améliore capture, fin de parole VAD, qualité de transcription, annulation et cycle de vie de l’interface.",
                    "Les enregistrements temporaires sont supprimés après usage sur PC et Mobile, y compris en cas d’interruption ou d’erreur.",
                    "L’arrêt TTS et la corrélation de lecture sont renforcés afin qu’une réponse terminée ne soit ni perdue ni prononcée deux fois.",
                    "Le raccourci vocal et le contraste ont été ajustés pour un accès quotidien plus clair.",
                ),
            ),
            ReleaseSection(
                "Language continuity",
                "Continuité de langue",
                (
                    "Each turn carries one authoritative locale from request through tools, progress, controlled refusals, and the canonical final answer.",
                    "PC and Mobile reconcile localized finals without mixing partial French and English fragments from different execution layers.",
                    "System diagnostics introduced by the new permission, document, browser, voice, and Autonomous flows are localized at their presentation boundary.",
                ),
                (
                    "Chaque tour transporte une locale autoritaire depuis la demande jusqu’aux outils, progrès, refus contrôlés et réponse finale canonique.",
                    "PC et Mobile réconcilient les réponses localisées sans mélanger des fragments français et anglais provenant de couches différentes.",
                    "Les diagnostics système des nouveaux parcours d’autorisations, documents, navigateur, voix et Autonomous sont localisés à leur frontière de présentation.",
                ),
            ),
            ReleaseSection(
                "Providers, models, reasoning, and ChatGPT Account",
                "Fournisseurs, modèles, raisonnement et Compte ChatGPT",
                (
                    "Reasoning levels are derived from the selected model’s real capabilities and mirrored consistently in PC settings, Autonomous, and Mobile.",
                    "Unsupported reasoning settings are normalized before provider calls instead of sending invalid payloads.",
                    "ChatGPT Account models join the configured model inventory used by Autonomous without pretending unavailable models are configured.",
                    "Supported native Codex actions pass through OSIA’s controlled bridge; unsupported actions retain the existing safe localized refusal contract.",
                    "Mobile OAuth callbacks use the stable OSIA relay flow, and client-secret Basic requests are normalized for compatible MCP providers.",
                ),
                (
                    "Les niveaux de raisonnement dérivent des capacités réelles du modèle et sont reflétés de façon cohérente dans les réglages PC, Autonomous et Mobile.",
                    "Les réglages de raisonnement non pris en charge sont normalisés avant l’appel fournisseur au lieu d’envoyer des payloads invalides.",
                    "Les modèles Compte ChatGPT rejoignent l’inventaire configuré d’Autonomous sans prétendre que des modèles indisponibles le sont.",
                    "Les actions natives Codex prises en charge passent par le pont contrôlé d’OSIA ; les autres conservent le contrat de refus localisé et sûr.",
                    "Les callbacks OAuth Mobile utilisent le relais OSIA stable et les requêtes Basic avec secret client sont normalisées pour les fournisseurs MCP compatibles.",
                ),
            ),
            ReleaseSection(
                "Personality and skills",
                "Personnalité et skills",
                (
                    "Personality changes are validated, persisted atomically, and synchronized without replacing newer canonical state with a stale phone copy.",
                    "Skill creation, enablement, editing, deletion, and execution state survive restart and reconnect with clearer typed failures.",
                    "PC and Mobile expose the same canonical personality and skills snapshot after reconciliation.",
                ),
                (
                    "Les changements de personnalité sont validés, persistés atomiquement et synchronisés sans remplacer un état canonique récent par une copie Mobile périmée.",
                    "Création, activation, modification, suppression et état d’exécution des skills survivent au redémarrage et à la reconnexion avec des erreurs typées plus claires.",
                    "PC et Mobile exposent le même instantané canonique de personnalité et de skills après réconciliation.",
                ),
            ),
            ReleaseSection(
                "Security and privacy",
                "Sécurité et confidentialité",
                (
                    "Permission decisions fail closed and are checked again at execution time rather than trusted only when a request is created.",
                    "Sensitive source paths, form values, private document metadata, and temporary media remain outside public receipts and conversation text.",
                    "Active content, external references, encrypted documents, DRM media, executable masquerades, and oversized containers are rejected before analysis.",
                    "OAuth callback logging is minimized and release tooling continues to keep credentials, certificates, keystores, and signing secrets outside Git and public artifacts.",
                ),
                (
                    "Les décisions d’autorisation échouent en sécurité et sont revérifiées au moment de l’exécution, pas uniquement à la création de la demande.",
                    "Chemins sources sensibles, valeurs de formulaires, métadonnées documentaires privées et médias temporaires restent hors des reçus publics et du texte de conversation.",
                    "Contenu actif, références externes, documents chiffrés, médias DRM, exécutables déguisés et conteneurs surdimensionnés sont refusés avant analyse.",
                    "La journalisation des callbacks OAuth est minimisée et les outils de release maintiennent identifiants, certificats, keystores et secrets de signature hors de Git et des artefacts publics.",
                ),
            ),
            ReleaseSection(
                "Installer, updater, packaging, and frozen Windows support",
                "Installateur, updater, packaging et Windows gelé",
                (
                    "Windows 1.1.0 uses a dedicated major_update token, explicit Major update badge, electric-purple-to-cyan treatment, and slow reduced-motion-aware breathing on relevant updater surfaces.",
                    "The update installer presents the complete scrollable changelog, update progress, preserved-data messaging, and completion state without showing fresh-install pages.",
                    "Frozen Windows support includes the managed Codex runtime, document and media workers, updater metadata, package smoke, and per-user integration paths.",
                    "The obsolete bundled Cloudflare tunnel path and proven legacy residue are removed while LAN, WebRTC, and the public relay remain the supported transport sequence.",
                    "In-place update rollback preserves the previous executable and payload until integration succeeds; settings, conversations, memory, pairing, skills, and provider state remain outside the install directory.",
                ),
                (
                    "Windows 1.1.0 utilise un token major_update dédié, un badge Mise à jour majeure, un traitement violet électrique vers cyan et une respiration lente respectant les animations réduites.",
                    "L’installateur de mise à jour présente le changelog complet et défilable, la progression, la conservation des données et la fin du parcours sans écrans de nouvelle installation.",
                    "Le support Windows gelé inclut le runtime Codex géré, les workers documents et médias, les métadonnées updater, le smoke package et les intégrations par utilisateur.",
                    "L’ancien chemin de tunnel Cloudflare embarqué et les résidus legacy prouvés sont retirés tandis que LAN, WebRTC puis relais public restent la séquence de transport prise en charge.",
                    "Le rollback de mise à jour conserve l’exécutable et le payload précédents jusqu’au succès de l’intégration ; réglages, conversations, mémoire, association, skills et fournisseurs restent hors du dossier d’installation.",
                ),
            ),
            ReleaseSection(
                "Unsigned Windows installer",
                "Installateur Windows non signé",
                (
                    "Windows 1.1.0 is distributed without an Authenticode signature, so Windows may show Unknown publisher or a Microsoft Defender SmartScreen warning.",
                    "The installer is distributed from the official OSIA HTTPS domain at download.osia.space.",
                    "Verify the installer against the SHA-256 published on osia.space before running it.",
                    "When you trust the official download, SmartScreen offers More info, then Run anyway; do not disable Windows security protections.",
                ),
                (
                    "Windows 1.1.0 est distribué sans signature Authenticode ; Windows peut donc afficher Éditeur inconnu ou un avertissement Microsoft Defender SmartScreen.",
                    "L’installateur est distribué depuis le domaine HTTPS officiel d’OSIA, download.osia.space.",
                    "Vérifiez l’installateur avec le SHA-256 publié sur osia.space avant de l’exécuter.",
                    "Si vous faites confiance au téléchargement officiel, SmartScreen propose Informations complémentaires puis Exécuter quand même ; ne désactivez pas les protections de sécurité Windows.",
                ),
            ),
            ReleaseSection(
                "Android UX and reliability",
                "UX et fiabilité Android",
                (
                    "Android 1.0.2 can scan the pairing QR code shown by the PC while retaining the existing explicit pairing-token checks.",
                    "Contrast, provider/model selection, Autonomous diagnostics, browser and document progress, and media attachment states are clearer on the phone.",
                    "The canonical permissions screen, Full Access confirmation journey, revocation, reconnect, and conversation reconciliation were validated on the physical Pixel release path.",
                    "Release packaging keeps the public package space.osia.app, the official release certificate, non-debuggable output, and no alternate test suffix.",
                ),
                (
                    "Android 1.0.2 peut scanner le QR d’association affiché par le PC tout en conservant les contrôles explicites du jeton d’association.",
                    "Contraste, sélection fournisseur/modèle, diagnostics Autonomous, progression navigateur/documents et états des médias joints sont plus clairs sur le téléphone.",
                    "Écran canonique d’autorisations, confirmation Full Access, révocation, reconnexion et réconciliation de conversation ont été validés sur le parcours release du Pixel physique.",
                    "Le packaging conserve le package public space.osia.app, le certificat release officiel, une sortie non débogable et aucun suffixe de test alternatif.",
                ),
            ),
            ReleaseSection(
                "Performance and dependency security",
                "Performances et sécurité des dépendances",
                (
                    "Document, PDF, browser, voice, and media workers enforce bounded time, memory, file, frame, archive, and output limits.",
                    "Progress-aware work avoids premature loop termination while real stalls still end deterministically.",
                    "Packaging and Worker tooling dependencies were updated and scanned; the relay itself is intentionally not deployed as part of this release.",
                ),
                (
                    "Les workers documents, PDF, navigateur, voix et médias imposent des limites de durée, mémoire, fichier, images, archive et sortie.",
                    "Le travail sensible à la progression évite les arrêts de boucle prématurés tandis que les blocages réels se terminent de façon déterministe.",
                    "Les dépendances de packaging et d’outillage Worker ont été mises à jour et scannées ; le relais lui-même n’est volontairement pas déployé dans cette release.",
                ),
            ),
            ReleaseSection(
                "Known limitations and unsupported formats",
                "Limites connues et formats non pris en charge",
                (
                    "The public desktop build targets Windows x64; no macOS, Linux, iOS, TestFlight, or App Store release is included.",
                    "Android remains a companion to a paired OSIA PC and does not run the full desktop agent independently.",
                    "Legacy binary XLS and PPT, encrypted or password-protected documents, DRM media, active macros/scripts, unsafe external references, and unsupported codecs are rejected rather than partially interpreted.",
                    "Browser automation is controlled and permission-bound; it does not guarantee success on every site, CAPTCHA, anti-bot flow, or irreversible external action.",
                    "Very large documents and media are bounded, paginated, sampled, or truncated with explicit markers; provider vision, audio, tool, and reasoning capabilities still vary by configured model.",
                ),
                (
                    "La build PC publique cible Windows x64 ; aucune release macOS, Linux, iOS, TestFlight ou App Store n’est incluse.",
                    "Android reste le compagnon d’un PC OSIA associé et n’exécute pas seul l’agent PC complet.",
                    "Anciens binaires XLS/PPT, documents chiffrés ou protégés, médias DRM, macros/scripts actifs, références externes dangereuses et codecs non pris en charge sont refusés plutôt qu’interprétés partiellement.",
                    "L’automatisation navigateur est contrôlée et soumise aux autorisations ; elle ne garantit pas tous les sites, CAPTCHA, protections anti-bot ou actions externes irréversibles.",
                    "Les documents et médias très volumineux sont bornés, paginés, échantillonnés ou tronqués avec marqueurs explicites ; les capacités vision, audio, outils et raisonnement varient encore selon le modèle configuré.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.0.6",
        released_on="2026-07-24",
        public=False,
        changes_en=(
            "ChatGPT Account now refuses unsupported native actions safely in the language of the current request, without executing or retrying them.",
            "Browser automation now tracks newly opened popups in real time and returns to the opener when they close.",
            "Long URLs and opaque identifiers now wrap inside conversation bubbles without clipping or changing copied text.",
        ),
        changes_fr=(
            "Compte ChatGPT refuse désormais en toute sécurité les actions natives non prises en charge dans la langue de la demande actuelle, sans les exécuter ni réessayer.",
            "L’automatisation du navigateur suit désormais les nouvelles fenêtres en temps réel et revient à la fenêtre d’origine lorsqu’elles se ferment.",
            "Les URL longues et les identifiants opaques passent désormais à la ligne dans les bulles de conversation, sans rognage ni modification du texte copié.",
        ),
    ),
    ReleaseEntry(
        version="1.0.5",
        released_on="2026-07-21",
        public=True,
        changes_en=(
            "Explore OSIA through three guide levels: Essential Getting Started, Master OSIA, and the developer-focused Advanced Guide.",
            "Attach files before sending on PC, then combine text, images, and multiple files in one message.",
            "Send images to compatible multimodal providers, with a clear refusal when the selected path does not support vision.",
            "Review update history offline, close OSIA from the main menu, and navigate Settings with independent scrolling.",
            "Responses combine careful reasoning with clearer explanations, alongside stability, update, and Windows distribution improvements.",
        ),
        changes_fr=(
            "Découvrez OSIA à travers trois niveaux : Démarrage essentiel, Maîtriser OSIA et le Guide avancé destiné aux développeurs.",
            "Joignez des fichiers avant l’envoi sur PC, puis combinez texte, images et plusieurs fichiers dans un même message.",
            "Transmettez les images aux fournisseurs multimodaux compatibles, avec un refus clair lorsque le chemin sélectionné ne prend pas en charge la vision.",
            "Consultez l’historique des mises à jour hors ligne, fermez OSIA depuis le menu principal et parcourez les réglages avec des défilements indépendants.",
            "Les réponses associent un raisonnement attentif à des explications plus claires, avec des améliorations de stabilité, de mise à jour et de distribution Windows.",
        ),
    ),
    ReleaseEntry(
        version="1.0.4",
        released_on="2026-07-20",
        public=True,
        changes_en=(
            "Updates prepare more safely and can roll back if something goes wrong.",
            "The Windows installer and update screens have a clearer design.",
            "The update journey keeps your installation choices.",
        ),
        changes_fr=(
            "Les mises à jour se préparent plus sûrement et peuvent revenir en arrière en cas de problème.",
            "L’installateur Windows et les écrans de mise à jour sont plus clairs.",
            "Le parcours de mise à jour conserve vos choix d’installation.",
        ),
    ),
    ReleaseEntry(
        version="1.0.3",
        released_on="2026-07-20",
        public=False,
        changes_en=(
            "Added a dedicated OSIA update screen.",
            "Kept installation choices automatically during updates.",
            "Internal build superseded by 1.0.4 before public release.",
        ),
        changes_fr=(
            "Ajout d’un écran dédié aux mises à jour d’OSIA.",
            "Conservation automatique des choix d’installation pendant les mises à jour.",
            "Version interne remplacée par la 1.0.4 avant toute publication.",
        ),
    ),
)

RELEASE_HISTORY = tuple(
    sorted(_RELEASES, key=lambda entry: parse_product_version(entry.version), reverse=True)
)

_INLINE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_LEADING_MARKUP_RE = re.compile(r"^(?:#{1,6}\s+|[-+*>]\s+|\d+[.)]\s+)")
_WHITESPACE_RE = re.compile(r"\s+")


def release_for_version(version: str) -> ReleaseEntry | None:
    """Return the embedded entry for ``version`` without doing any I/O."""

    return next((entry for entry in RELEASE_HISTORY if entry.version == version), None)


def manifest_release_notes(version: str) -> list[str]:
    """Return plain English notes for the release manifest generator."""

    entry = release_for_version(version)
    return list(entry.summary("en")) if entry is not None else []


MAJOR_UPDATE_VERSIONS = frozenset({"1.1.0"})


def is_major_update(version: str | None) -> bool:
    return isinstance(version, str) and version in MAJOR_UPDATE_VERSIONS


def installer_release_notes(version: str, language: str) -> str:
    """Render the complete localized notes for the visible Windows updater."""

    entry = release_for_version(version)
    if entry is None:
        return ""
    lines: list[str] = []
    for title, changes in entry.note_sections(language):
        if title:
            if lines:
                lines.append("")
            lines.append(title.upper())
            lines.append("")
        lines.extend(f"• {change}" for change in changes)
    return "\n".join(lines).strip() + "\n"


def normalize_remote_notes(notes) -> tuple[str, ...]:
    """Normalize already-validated remote notes into bounded plain text.

    The updater rejects literal angle brackets at the manifest boundary.  This
    final presentation guard also decodes entities, removes any encoded tags,
    and strips common Markdown decoration before text reaches a Qt label.
    """

    normalized: list[str] = []
    for raw in notes or ():
        if not isinstance(raw, str):
            continue
        text = html.unescape(raw)
        text = _HTML_TAG_RE.sub("", text)
        text = _INLINE_LINK_RE.sub(r"\1", text)
        text = _LEADING_MARKUP_RE.sub("", text.strip())
        text = (
            text.replace("*", "").replace("_", "").replace("`", "")
            .replace("~~", "")
        )
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if text:
            normalized.append(text[:240])
    return tuple(normalized[:5])


__all__ = [
    "MAJOR_UPDATE_VERSIONS",
    "RELEASE_HISTORY",
    "ReleaseEntry",
    "ReleaseSection",
    "installer_release_notes",
    "is_major_update",
    "manifest_release_notes",
    "normalize_remote_notes",
    "release_for_version",
]

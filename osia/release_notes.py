"""Embedded, localized release history for the desktop application.

Remote updater notes describe a newer available build.  This module remains
the offline source of truth for versions already shipped with OSIA, so the
installed version's notes survive cache cleanup and network outages.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from osia.version import HISTORICAL_DISPLAY_VERSIONS, parse_product_version, display_historical_text


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
        version="0.9.261", released_on="2026-11-09", public=True,
        changes_en=(
            "The Linux installer includes the complete Codex runtime so ChatGPT Account can call OSIA tools.",
        ),
        changes_fr=(
            "L’installateur Linux inclut le runtime Codex complet pour que le compte ChatGPT puisse appeler les outils OSIA.",
        ),
    ),
    ReleaseEntry(
        version="0.9.260", released_on="2026-11-08", public=True,
        changes_en=(
            "ChatGPT sign-in continues when OSIA Mobile reconnects after opening the browser.",
            "OSIA refreshes the ChatGPT account shared by the desktop and mobile clients after sign-in or sign-out.",
        ),
        changes_fr=(
            "La connexion ChatGPT continue lorsque le mobile se reconnecte après avoir ouvert le navigateur.",
            "OSIA actualise le compte ChatGPT partagé par le bureau et le mobile après la connexion ou la déconnexion.",
        ),
    ),
    ReleaseEntry(
        version="0.9.259", released_on="2026-11-08", public=True,
        changes_en=(
            "The Linux x86-64 beta uses the shared OSIA runtime. The installer keeps data in your profile and retains installed versions for manual rollback.",
        ),
        changes_fr=(
            "La bêta Linux x86‑64 utilise le moteur OSIA commun. L’installateur conserve les données dans ton profil et garde les versions installées pour un retour manuel.",
        ),
    ),
    ReleaseEntry(
        version="0.9.258", released_on="2026-11-08", public=True,
        changes_en=(
            "ChatGPT Account receives the result of a slow browser action in the same turn. Recent browser results remain available after context compaction.",
            "Browser page text is read in consecutive chunks. OSIA can verify what was saved using the current page after a wait expires.",
            "OSIA favors existing tabs when verifying web changes and identifies an already open matching tab when a separate tab is requested.",
        ),
        changes_fr=(
            "ChatGPT Account reçoit le résultat d’une action navigateur lente dans le même tour. Les derniers résultats restent accessibles après la compaction du contexte.",
            "OSIA lit le texte des pages par blocs consécutifs. Après un délai dépassé, il peut vérifier l’enregistrement à partir de la page actuelle.",
            "OSIA privilégie les onglets existants pour vérifier une modification et signale un onglet correspondant si un nouvel onglet est demandé.",
        ),
    ),
    ReleaseEntry(
        version="0.9.257", released_on="2026-11-07", public=True,
        changes_en=(
            "Each OSIA conversation keeps its ChatGPT Account thread across turns. The managed browser reuses an existing tab when the requested address is already open.",
            "The chat shows context usage beside Send. Use /compact to summarize earlier exchanges. Automatic compaction keeps the active request and recent work.",
            "OSIA reads browser form fields together and checks each field reference before filling it. A timed-out read can be retried in the same tab.",
        ),
        changes_fr=(
            "Chaque discussion OSIA conserve son fil ChatGPT Account d’un tour à l’autre. Le navigateur géré réutilise l’onglet quand l’adresse demandée est déjà ouverte.",
            "Le chat affiche le contexte utilisé à côté du bouton Envoyer. La commande /compact résume les anciens échanges. La compaction automatique conserve la demande en cours et les travaux récents.",
            "OSIA lit les champs des formulaires en groupe et vérifie chaque référence avant de saisir du texte. Une lecture expirée peut être relancée dans le même onglet.",
        ),
    ),
    ReleaseEntry(
        version="0.9.256", released_on="2026-11-07", public=True,
        changes_en=(
            "ChatGPT Account can use buttons and forms in the managed browser in the background, including when mouse control is disabled.",
            "OSIA can scroll panels and read selected tables and lists. Web actions check the current target and verify new downloads.",
            "MCP discovers tools across all catalog pages. OSIA validates tool arguments before dispatch and reads their actual execution status.",
            "Scheduled tasks can continue after an uncertain result. Short model contexts keep the active turn while summarizing older exchanges.",
            "Improved local model discovery, voice playback and the conversation list. Windows updates pick the installer for the selected release.",
        ),
        changes_fr=(
            "ChatGPT Account peut utiliser les boutons et formulaires du navigateur géré en arrière-plan, même si la souris est désactivée.",
            "OSIA peut faire défiler les panneaux et lire les tableaux et listes ciblés. Il vérifie la cible des actions web et les nouveaux téléchargements.",
            "MCP détecte les outils sur toutes les pages du catalogue. OSIA valide les arguments avant exécution et lit le statut réel des outils.",
            "Les tâches planifiées peuvent continuer après un résultat incertain. Les modèles à contexte court gardent le tour actif et résument les anciens échanges.",
            "La détection des modèles locaux, la lecture vocale et la liste des discussions sont améliorées. Windows utilise l’installateur de la version choisie.",
        ),
    ),
    ReleaseEntry(
        version="0.9.255", released_on="2026-11-06", public=True,
        changes_en=(
            "OSIA reviews progress before ending a task that used tools and can continue unfinished work in the same turn.",
            "On Windows, OSIA can capture the requested window in the background and brings it forward before using the mouse.",
            "Use /mousecontrol to toggle mouse control for the current conversation while keeping vision and keyboard input available.",
            "The chat menu adds skills, model and reasoning selection, and conversation mentions with @.",
            "OSIA can attach screenshots to replies and carry images forward in the conversation. Typing accented characters on Windows is fixed.",
        ),
        changes_fr=(
            "OSIA vérifie l’avancement avant de terminer une tâche qui utilise des outils et peut reprendre le travail dans le même tour.",
            "Sous Windows, OSIA peut capturer la fenêtre demandée en arrière-plan et la place au premier plan avant d’utiliser la souris.",
            "La commande /mousecontrol active ou désactive la souris pour la discussion en cours. La vision et le clavier restent disponibles.",
            "Le menu du chat permet de choisir un skill, un modèle et son niveau de raisonnement, ou de citer une discussion avec @.",
            "OSIA peut joindre des captures à ses réponses et réutiliser les images de la discussion. La saisie des accents sous Windows est corrigée.",
        ),
    ),
    ReleaseEntry(
        version="0.9.249", released_on="2026-11-06", public=True,
        changes_en=(
            "Windows updates use the standard download address so earlier Preview clients can find the new installer.",
            "OSIA removes older cached Windows installers after the new installer has been downloaded and verified.",
        ),
        changes_fr=(
            "Les mises à jour Windows utilisent l’adresse standard pour que les anciens clients Preview puissent trouver le nouvel installateur.",
            "OSIA supprime les anciens installateurs Windows en cache après le téléchargement et la vérification du nouveau fichier.",
        ),
    ),
    ReleaseEntry(
        version="0.9.248", released_on="2026-11-06", public=True,
        changes_en=(
            "Fixed the Windows upgrade check for earlier OSIA installations with a missing release sequence.",
            "OSIA 0.9.0248 can search and read public web pages without opening your browser. It can still open pages when a task requires interaction.",
            "Messages that begin with Markdown headings stay in conversation history and are available on later turns.",
            "Desktop settings show the floating capsule shortcut, add a Donation section and give update cards a subtle outline.",
        ),
        changes_fr=(
            "Correction de la mise à jour Windows lorsque l’ancienne installation d’OSIA n’a pas de numéro de séquence.",
            "OSIA 0.9.0248 peut rechercher et lire des pages publiques sans ouvrir votre navigateur. Il peut toujours ouvrir une page si la tâche demande d’interagir avec elle.",
            "Les messages qui commencent par un titre Markdown sont conservés dans l’historique et restent disponibles aux tours suivants.",
            "Les paramètres indiquent le raccourci de la capsule et ajoutent une section Don. Les cartes de mise à jour ont un contour discret.",
        ),
    ),
    ReleaseEntry(
        version="0.9.1", released_on="2026-11-05", public=True,
        changes_en=(
            "Updated the ChatGPT Account runtime to Codex 0.166.0 with GPT-6.1 Sol and Terra on eligible accounts. OSIA checks daily for compatible stable Codex releases.",
            "Added OpenRouter and improved discovery of local Ollama and LM Studio models.",
            "Refined desktop settings and conversations with clearer controls, smoother transitions and simpler dropdowns.",
        ),
        changes_fr=(
            "Codex 0.166.0 donne accès à GPT-6.1 Sol et Terra sur les comptes éligibles. OSIA vérifie chaque jour les nouvelles versions stables compatibles de Codex.",
            "Ajout d’OpenRouter et détection améliorée des modèles locaux Ollama et LM Studio.",
            "Paramètres et discussions affinés, avec des commandes plus lisibles, des transitions fluides et des listes déroulantes simplifiées.",
        ),
    ),
    ReleaseEntry(
        version="0.9.0", released_on=None, public=False,
        changes_en=(
            "Public beta source candidate; installer publication awaits operator acceptance.",
            "Keep one Thinking section above each assistant response, with retained public commentary and exact turn ownership.",
            "Adopt the official product versions and historical display aliases; preserve original artifact identities and user data.",
            "Use explicit Windows release chronology. Legacy clients require the reviewed standalone installer; existing channel manifests remain unchanged.",
        ),
        changes_fr=(
            "Candidat source bêta publique ; publication après validation opérateur.",
            "Un seul Thinking au-dessus de chaque réponse, avec commentaires publics conservés et identité exacte du tour.",
            "Versions officielles et alias historiques ; identités des artefacts et données utilisateur conservées.",
            "Chronologie Windows explicite. Les anciens clients nécessitent l’installateur autonome validé ; manifestes existants inchangés.",
        ),
    ),
    ReleaseEntry(
        version="1.1.8",
        released_on="2026-10-18",
        public=False,
        changes_en=(
            "Development test candidate: native Claude Messages streaming, public Thinking and canonical OSIA tool results, using Console API billing.",
            "Fix bundled Windows ChatGPT Account startup with version-compatible Codex feature gates; preserve the working source sign-in flow.",
            "Strengthen frozen provider startup/restart and offline tool-boundary checks. Real account sign-in and installed acceptance remain operator tests.",
            "Unsigned internal preview. Stable 1.1.7 remains unchanged and is the rollback version.",
        ),
        changes_fr=(
            "Candidat de test : streaming Claude Messages natif, Thinking public et résultats des outils OSIA, avec facturation API Console.",
            "Corrige le démarrage ChatGPT Account Windows empaqueté avec des flags Codex adaptés à la version ; conserve la connexion source fonctionnelle.",
            "Renforce les contrôles du runtime empaqueté et des échanges d’outils hors ligne. La connexion réelle et la validation installée restent à tester par l’opérateur.",
            "Préversion interne non signée. Stable 1.1.7 reste inchangée et disponible pour revenir en arrière.",
        ),
    ),
    ReleaseEntry(
        version="1.1.7",
        released_on="2026-10-07",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Short and multi-step requests stay in one durable Chat runtime, with no handoff to a separate Autonomous engine.",
            "The selected provider/model remains the turn's semantic authority; dispatch and observed outcomes stay explicit, and unknown remains unknown.",
            "Computer Use V4 checks fresh surfaces and verifies through screenshot → action → screenshot; OUTCOME_UNKNOWN stays factual and a distinct fresh model request remains available.",
            "Desktop conversations gain project/folder actions and simpler settings; Thinking uses selected-model narration, and DeepSeek offers only deepseek-flash.",
            "Windows-only, NotSigned, no Mobile publication; approved legal bundle 2026-10-05.4 is included and Windows 1.1.6 remains rollback.",
        ),
        summary_fr=(
            "Les demandes simples ou longues restent dans un runtime Chat durable, sans transfert vers un moteur Autonomous distinct.",
            "Le fournisseur/modèle sélectionné garde l'autorité sémantique du tour ; les résultats observés restent explicites et unknown reste inconnu.",
            "Computer Use V4 vérifie les surfaces récentes puis suit capture → action → capture ; OUTCOME_UNKNOWN reste factuel et un nouvel appel distinct du modèle reste possible.",
            "Les conversations gagnent les actions projet/dossier et des réglages simplifiés ; Thinking reprend le modèle sélectionné et DeepSeek expose deepseek-flash.",
            "Windows uniquement, NotSigned, sans publication Mobile ; le bundle juridique 2026-10-05.4 est inclus et 1.1.6 reste le rollback.",
        ),
        sections=(
            ReleaseSection(
                "One canonical Chat runtime",
                "Un runtime Chat canonique",
                (
                    "Short and multi-step requests stay in the same durable Chat turn; a separate Autonomous runtime does not take over the work.",
                    "The selected provider and model remain the semantic authority. ToolAttempt and ActionTruth record dispatch and observed outcomes; OUTCOME_UNKNOWN stays unknown.",
                ),
                (
                    "Les demandes simples ou en plusieurs étapes restent dans le même tour Chat durable ; aucun runtime Autonomous distinct ne reprend le travail.",
                    "Le fournisseur et le modèle sélectionnés gardent l'autorité sémantique. ToolAttempt et ActionTruth consignent le dispatch et les résultats observés ; OUTCOME_UNKNOWN reste inconnu.",
                ),
            ),
            ReleaseSection(
                "Reliable tools and Computer Use V4",
                "Outils fiables et Computer Use V4",
                (
                    "Computer Use V4 checks target and surface freshness, then verifies through screenshot → action → screenshot; stale frames and windows are rejected.",
                    "Cancellation stays correlated to the exact turn. OUTCOME_UNKNOWN remains factual; a distinct fresh model-requested call remains allowed without a required reconciliation step.",
                ),
                (
                    "Computer Use V4 vérifie l'actualité de la cible et de la surface, puis confirme par capture → action → capture ; les fenêtres et images obsolètes sont rejetées.",
                    "L'annulation reste corrélée au tour exact. OUTCOME_UNKNOWN reste factuel ; un nouvel appel distinct du modèle reste possible sans étape obligatoire de réconciliation.",
                ),
            ),
            ReleaseSection(
                "Desktop experience and providers",
                "Expérience Bureau et fournisseurs",
                (
                    "Ordinary available tools remain usable without a permission broker, while protection for destructive operations against critical system paths remains narrowly scoped.",
                    "Conversation project/folder actions, typed slash commands, guide improvements, contained settings scrolling, and deletion rollback refine the desktop experience.",
                    "Thinking narration comes from the selected model response; the local continuous-learning path remains in the canonical runtime, and DeepSeek exposes only deepseek-flash.",
                ),
                (
                    "Les outils disponibles restent utilisables sans courtier de permissions ; la protection contre les opérations destructrices sur les chemins système critiques reste étroitement limitée.",
                    "Les actions projet/dossier, commandes slash typées, améliorations du guide, défilement contenu des réglages et retour arrière de suppression affinent l'expérience Bureau.",
                    "La narration Thinking provient de la réponse du modèle sélectionné ; l'apprentissage continu local reste dans le runtime canonique et DeepSeek expose uniquement deepseek-flash.",
                ),
            ),
            ReleaseSection(
                "Legal and Windows release state",
                "État juridique et release Windows",
                (
                    "Includes the approved WOR-8 legal bundle 2026-10-05.4, effective 2026-10-06; the approved legal text is unchanged.",
                    "Windows-only and NotSigned under the current code-signing status; Windows 1.1.6 remains the previous public rollback and no Mobile release is included.",
                ),
                (
                    "Inclut le bundle juridique WOR-8 approuvé 2026-10-05.4, en vigueur le 2026-10-06 ; le texte juridique approuvé reste inchangé.",
                    "Windows uniquement et NotSigned selon le statut actuel de signature ; Windows 1.1.6 reste le rollback public précédent et aucune release Mobile n'est incluse.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.6",
        released_on="2026-08-07",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Windows 1.1.6 durably reconciles delayed tool outcomes without replaying possible external effects.",
            "The external shadow around the OSIA PC window is removed while the existing compact and expanded layouts remain intact.",
            "Updates initiated by 1.1.6 show real download, verification, and launch progress; the legacy 1.1.5 download UI is unchanged.",
            "Canonical per-turn language enforcement keeps short French Chat replies French across repair, persistence, and speech.",
            "Windows 1.1.5 remains the immutable rollback; 1.1.6 is Windows-only and NotSigned, with no Mobile publication.",
        ),
        summary_fr=(
            "Windows 1.1.6 réconcilie durablement les résultats d’outils retardés sans rejouer un effet externe possible.",
            "L’ombre externe autour de la fenêtre PC OSIA est supprimée sans modifier les dispositions compacte et élargie existantes.",
            "Les mises à jour lancées par 1.1.6 affichent la progression réelle du téléchargement, de la vérification et du lancement ; l’UI 1.1.5 reste inchangée.",
            "La langue canonique par tour maintient les réponses Chat françaises courtes en français lors de la réparation, de la persistance et de la synthèse vocale.",
            "Windows 1.1.5 reste le rollback immuable ; 1.1.6 concerne uniquement Windows, reste NotSigned et n’inclut aucune publication Mobile.",
        ),
        sections=(
            ReleaseSection(
                "Durable tool reconciliation",
                "Réconciliation durable des outils",
                (
                    "A slow tool can release the Chat turn while its canonical attempt remains durably reconcilable after completion or restart.",
                    "Late results are verified once and possible or unknown external effects are never replayed merely to obtain a terminal answer.",
                ),
                (
                    "Un outil lent peut libérer le tour Chat tandis que sa tentative canonique reste réconciliable durablement après achèvement ou redémarrage.",
                    "Les résultats tardifs sont vérifiés une seule fois et les effets externes possibles ou inconnus ne sont jamais rejoués uniquement pour obtenir une réponse terminale.",
                ),
            ),
            ReleaseSection(
                "Windows window and updater state",
                "Fenêtre Windows et état de l’updater",
                (
                    "The PC window no longer draws an external operating-system shadow, while its existing shape, size transitions, and controls are preserved.",
                    "The 1.1.6 updater reports bytes received, verification, readiness, launch, interruption, retry, and indeterminate-length states without duplicate downloads.",
                ),
                (
                    "La fenêtre PC n’affiche plus d’ombre externe du système, tout en préservant sa forme, ses transitions de largeur et ses contrôles existants.",
                    "L’updater 1.1.6 affiche les octets reçus, la vérification, la disponibilité, le lancement, l’interruption, le nouvel essai et les tailles indéterminées sans double téléchargement.",
                ),
            ),
            ReleaseSection(
                "Canonical language per turn",
                "Langue canonique par tour",
                (
                    "Each Chat and Autonomous turn keeps one canonical target language across provider output, repair, persistence, projection, and TTS.",
                    "Short or ambiguous French messages use the current conversation and client locale so accidental English output is repaired once without replaying tools.",
                ),
                (
                    "Chaque tour Chat et Autonomous conserve une langue cible canonique pour la sortie fournisseur, la réparation, la persistance, la projection et le TTS.",
                    "Les messages français courts ou ambigus utilisent la conversation et la langue du client ; une sortie anglaise accidentelle est réparée une fois sans rejouer d’outil.",
                ),
            ),
            ReleaseSection(
                "Windows-only release compatibility",
                "Compatibilité de la release Windows uniquement",
                (
                    "Version, packaging, updater, and release metadata are aligned on Windows 1.1.6 with strict host, path, size, and SHA-256 validation.",
                    "Windows 1.1.5 remains the verified immutable rollback; Windows 1.1.6 remains NotSigned with no publisher claim and includes no Mobile publication.",
                ),
                (
                    "Version, packaging, updater et métadonnées de release sont alignés sur Windows 1.1.6 avec validation stricte de l’hôte, du chemin, de la taille et du SHA-256.",
                    "Windows 1.1.5 reste le rollback immuable vérifié ; Windows 1.1.6 reste NotSigned sans éditeur revendiqué et n’inclut aucune publication Mobile.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.5",
        released_on="2026-08-06",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Windows 1.1.5 isolates Chat activity from Autonomous runs so Send and Stop remain bound to the exact conversation, request, and turn.",
            "Autonomous Abandon is exact and idempotent across active, blocked, and failed runs while preserving completed and failed terminal truth.",
            "A possible or unknown external effect remains fail-closed and is never replayed merely to complete abandonment.",
            "Reconnect and restart restore Chat and Autonomous independently without duplicating a mission, provider turn, or tool effect.",
            "This is a Windows-only release; the matching Mobile UI correction was validated locally, but no Mobile publication is included.",
        ),
        summary_fr=(
            "Windows 1.1.5 isole l’activité Chat des runs Autonomous afin que Send et Stop restent liés à la conversation, la requête et au tour exacts.",
            "Abandon Autonomous est exact et idempotent pour les runs actifs, bloqués ou en échec, tout en préservant la vérité terminale.",
            "Un effet externe possible ou inconnu reste fail-closed et n’est jamais rejoué uniquement pour terminer l’abandon.",
            "Reconnexion et redémarrage restaurent Chat et Autonomous séparément sans dupliquer mission, tour fournisseur ni effet d’outil.",
            "Cette release concerne Windows uniquement ; la correction UI Mobile correspondante a été validée localement sans publication Mobile.",
        ),
        sections=(
            ReleaseSection(
                "Independent Chat and Autonomous state",
                "États Chat et Autonomous indépendants",
                (
                    "Chat activity is keyed only to its exact conversation, request, and turn; a successful handoff releases the originating Chat turn immediately.",
                    "An active or failed Autonomous run never disables a new Chat send, and Chat Stop can cancel only the current Chat turn.",
                    "One Autonomous run may block a second Autonomous run, while Autonomous Abandon targets only the exact run and never Chat.",
                ),
                (
                    "L’activité Chat dépend uniquement de sa conversation, de sa requête et de son tour exacts ; un handoff réussi libère immédiatement le tour Chat d’origine.",
                    "Un run Autonomous actif ou en échec ne désactive jamais un nouvel envoi Chat, et Stop Chat annule uniquement le tour Chat courant.",
                    "Un run Autonomous peut en bloquer un second, tandis qu’Abandon Autonomous cible uniquement le run exact et jamais Chat.",
                ),
            ),
            ReleaseSection(
                "Truthful and durable abandonment",
                "Abandon durable et fidèle",
                (
                    "Abandon is idempotent from running, waiting, verifying, blocked, and failed states, with requested and proven states persisted separately.",
                    "Completed and failed terminal outcomes remain unchanged while abandonment records whether no effect, a confirmed effect, or an unknown effect is possible.",
                    "A possible or unknown external effect is never replayed, retried, or replanned merely to prove workflow abandonment.",
                ),
                (
                    "Abandon est idempotent depuis les états running, waiting, verifying, blocked et failed, avec demande et preuve persistées séparément.",
                    "Les résultats terminaux completed et failed restent inchangés tandis que l’abandon distingue absence d’effet, effet confirmé ou effet inconnu possible.",
                    "Un effet externe possible ou inconnu n’est jamais rejoué, réessayé ni replannifié uniquement pour prouver l’abandon du workflow.",
                ),
            ),
            ReleaseSection(
                "Windows release and compatibility",
                "Release Windows et compatibilité",
                (
                    "Version, packaging, updater, and release metadata remain aligned on Windows 1.1.5 with strict host, path, size, and SHA-256 validation.",
                    "Windows 1.1.4 remains the validated rollback and immutable fallback; Windows 1.1.5 remains NotSigned with no publisher claim under the authorized policy.",
                    "No Mobile publication is part of this Windows release; the locally validated Mobile UI build retains its existing versionCode and is not announced as public.",
                ),
                (
                    "Version, packaging, updater et métadonnées restent alignés sur Windows 1.1.5 avec validation stricte de l’hôte, du chemin, de la taille et du SHA-256.",
                    "Windows 1.1.4 reste le rollback validé et le fallback immuable ; Windows 1.1.5 reste NotSigned sans éditeur revendiqué selon la politique autorisée.",
                    "Aucune publication Mobile ne fait partie de cette release Windows ; le build UI Mobile validé localement conserve son versionCode et n’est pas annoncé public.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.4",
        released_on="2026-08-06",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Windows 1.1.4 improves the reliability and correlation of cancellation requests received from compatible Mobile clients.",
            "Cancellation controls target the exact active conversation, turn, and mission so normal Chat and Autonomous work remain separate.",
            "Existing Mobile clients and legacy textual cancellation remain compatible; no new Mobile interface is required for this Windows release.",
            "Release maintenance preserves single-dispatch, fail-closed behavior and the existing updater compatibility policy.",
            "This release is Windows-only. The next Mobile update will be published separately.",
        ),
        summary_fr=(
            "Windows 1.1.4 améliore la fiabilité et la corrélation des demandes d’annulation reçues depuis les clients Mobile compatibles.",
            "Les contrôles d’annulation ciblent exactement la conversation, le tour et la mission actifs afin de séparer Chat normal et Autonomous.",
            "Les clients Mobile existants et l’annulation textuelle historique restent compatibles ; aucune nouvelle interface Mobile n’est requise.",
            "La maintenance de cette publication préserve l’envoi unique, le comportement fail-closed et la politique de compatibilité de l’updater.",
            "Cette publication concerne Windows uniquement. La prochaine mise à jour Mobile sera publiée séparément.",
        ),
        sections=(
            ReleaseSection(
                "Correlated cancellation control",
                "Contrôle d’annulation corrélé",
                (
                    "The Windows server accepts an additive cancellation control scoped to the active conversation, turn, and mission identifiers.",
                    "A stale or mismatched target fails safely and cannot cancel unrelated Chat or Autonomous work.",
                    "Repeated cancellation controls remain idempotent and do not create a provider turn, a second mission, or duplicate work.",
                ),
                (
                    "Le serveur Windows accepte un contrôle d’annulation additif limité aux identifiants de conversation, de tour et de mission actifs.",
                    "Une cible obsolète ou incohérente échoue de façon sûre et ne peut pas annuler un autre travail Chat ou Autonomous.",
                    "Les contrôles d’annulation répétés restent idempotents et ne créent ni tour fournisseur, ni seconde mission, ni travail dupliqué.",
                ),
            ),
            ReleaseSection(
                "Chat, Autonomous, and existing Mobile compatibility",
                "Compatibilité Chat, Autonomous et Mobile existante",
                (
                    "Precise targeting keeps normal Chat and Autonomous cancellation state distinct while preserving the existing public server contract.",
                    "Current Mobile clients and legacy textual cancellation remain supported; Windows 1.1.4 does not require a new Mobile interface.",
                    "The additive Windows compatibility support does not publish a separate Mobile release or change the Mobile interface.",
                ),
                (
                    "Le ciblage précis sépare les états d’annulation de Chat normal et Autonomous tout en préservant le contrat serveur public existant.",
                    "Les clients Mobile actuels et l’annulation textuelle historique restent pris en charge ; Windows 1.1.4 n’exige pas de nouvelle interface Mobile.",
                    "Ce support de compatibilité Windows additif ne publie pas de release Mobile séparée et ne modifie pas l’interface Mobile.",
                ),
            ),
            ReleaseSection(
                "Windows-only release maintenance",
                "Maintenance de publication Windows uniquement",
                (
                    "Version, packaging, updater, and release metadata remain aligned on Windows 1.1.4 with strict host, path, size, and SHA-256 validation.",
                    "Windows 1.1.3 remains the validated rollback; Windows 1.1.4 remains NotSigned with no publisher claim under the authorized general policy.",
                    "This release is Windows-only. The next Mobile update will be published separately.",
                ),
                (
                    "Version, packaging, updater et métadonnées de publication restent alignés sur Windows 1.1.4 avec validation stricte de l’hôte, du chemin, de la taille et du SHA-256.",
                    "Windows 1.1.3 reste le rollback validé ; Windows 1.1.4 reste NotSigned sans éditeur revendiqué selon la politique générale autorisée.",
                    "Cette publication concerne Windows uniquement. La prochaine mise à jour Mobile sera publiée séparément.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.3",
        released_on="2026-08-04",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Long-running and uncertain tool actions now use a durable attempt lifecycle with waiting, verification, and same-turn resumption instead of redispatch.",
            "Sleep/wake and Mobile reconnect restore the existing attempt and suppress false Retry prompts, duplicate effects, and fallback execution.",
            "Normal Chat and Autonomous now share one orchestration, and Chat-to-Autonomous handoff attaches to an existing attempt instead of creating another action.",
            "Cancellation now distinguishes before-dispatch, requested, verified, and unknown-effect states while preserving precise fail-closed results.",
            "Provider/model continuity, anti-replay, no-fallback behavior, and localized waiting, verifying, resuming, and blocked states persist across recovery.",
        ),
        summary_fr=(
            "Les actions longues ou incertaines utilisent désormais un cycle de vie durable des tentatives avec attente, vérification et reprise du même tour sans renvoi.",
            "Veille/réveil et reconnexion Mobile restaurent la tentative existante et évitent faux Retry, effets dupliqués et exécution de fallback.",
            "Chat normal et Autonomous partagent une orchestration ; le transfert Chat vers Autonomous s’attache à la tentative existante sans recréer l’action.",
            "L’annulation distingue avant envoi, demandée, vérifiée et effet inconnu, tout en conservant des résultats précis et fail-closed.",
            "Continuité fournisseur/modèle, anti-rejeu, absence de fallback et états localisés d’attente, vérification, reprise et blocage persistent après récupération.",
        ),
        sections=(
            ReleaseSection(
                "Durable attempts and same-turn resumption",
                "Tentatives durables et reprise du même tour",
                (
                    "A durable tool-attempt lifecycle records acceptance, dispatch, waiting, verification, terminal outcome, and the exact mutation identity across process and transport interruptions.",
                    "A delayed or uncertain result resumes the same turn from its existing attempt after verification instead of asking the provider to issue the action again.",
                    "Timeout no longer implies failure or authorizes a duplicate action; an unknown effect remains blocked until verification proves the outcome or the bounded deadline expires.",
                    "Normal Chat and Autonomous use the same attempt store, reconciliation rules, and terminal-result contract rather than parallel recovery implementations.",
                ),
                (
                    "Un cycle de vie durable des tentatives d’outil conserve acceptation, envoi, attente, vérification, résultat terminal et identité exacte de mutation malgré les coupures de processus ou de transport.",
                    "Un résultat retardé ou incertain reprend le même tour depuis sa tentative existante après vérification, sans demander au fournisseur de réémettre l’action.",
                    "Un timeout ne signifie plus échec et n’autorise aucune action dupliquée ; un effet inconnu reste bloqué jusqu’à preuve du résultat ou expiration de la limite de vérification.",
                    "Chat normal et Autonomous utilisent le même store de tentatives, les mêmes règles de réconciliation et le même contrat de résultat terminal, sans récupération parallèle.",
                ),
            ),
            ReleaseSection(
                "Recovery across devices and orchestration",
                "Récupération entre appareils et orchestration",
                (
                    "Windows sleep and wake recovery keeps conversation, turn, request, attempt, provider, and model identity while restoring waiting and delivering one final response.",
                    "Mobile reconnect recovery restores the active waiting state without a false Retry control, a new request, or a second tool dispatch.",
                    "Autonomous wake and restart reconcile the durable attempt before any retry or replan and keep an unknown external effect fail-closed.",
                    "Chat-to-Autonomous handoff attaches the mission to the existing attempt and preserves one canonical action, one mutation, and one final outcome.",
                ),
                (
                    "La récupération après veille et réveil Windows conserve conversation, tour, requête, tentative, fournisseur et modèle, restaure l’attente et livre une seule réponse finale.",
                    "La reconnexion Mobile restaure l’attente active sans faux contrôle Retry, nouvelle requête ni second envoi d’outil.",
                    "Le réveil ou redémarrage Autonomous réconcilie la tentative durable avant tout retry ou replan et maintient fail-closed un effet externe inconnu.",
                    "Le transfert Chat vers Autonomous attache la mission à la tentative existante et conserve une action canonique, une mutation et un résultat final uniques.",
                ),
            ),
            ReleaseSection(
                "Precise cancellation and unknown outcomes",
                "Annulation précise et résultats inconnus",
                (
                    "Cancellation before dispatch proves that no effect occurred, executes no mutation, and reports the exact cancelled-before-effect result.",
                    "Cancellation requested after dispatch does not pretend the action stopped; OSIA waits for verification and reports the verified success or fail-closed unknown outcome.",
                    "Verification-deadline expiry produces a precise unknown-after-start diagnosis without replay, retry, replan, provider fallback, or a misleading success or failure claim.",
                ),
                (
                    "L’annulation avant envoi prouve l’absence d’effet, n’exécute aucune mutation et renvoie le résultat exact annulé avant effet.",
                    "Une annulation demandée après envoi ne prétend pas que l’action s’est arrêtée ; OSIA attend la vérification puis rapporte le succès vérifié ou le résultat inconnu fail-closed.",
                    "L’expiration de la vérification produit un diagnostic précis d’effet inconnu après démarrage, sans rejeu, retry, replan, fallback fournisseur ni faux succès ou échec.",
                ),
            ),
            ReleaseSection(
                "Continuity, anti-replay, and localized state",
                "Continuité, anti-rejeu et états localisés",
                (
                    "The exact provider and model remain pinned through waiting, verification, reconnect, restart, handoff, and finalization, with no fallback to another model or provider.",
                    "Mutation authority stays single-use across Chat, Autonomous, LAN, WebRTC, and relay recovery so rerenders, reconnects, and late completions cannot replay an effect.",
                    "Waiting, verifying, resuming, cancellation, success, and blocked-unknown states are projected consistently to PC and Mobile with localized English and French copy.",
                    "Windows 1.1.2 remains the validated rollback; Windows 1.1.3 remains NotSigned with no publisher claim and preserves the bounded official unsigned-Stable policy.",
                ),
                (
                    "Le fournisseur et modèle exacts restent figés pendant attente, vérification, reconnexion, redémarrage, transfert et finalisation, sans aucun fallback vers un autre choix.",
                    "L’autorité de mutation reste à usage unique entre Chat, Autonomous, LAN, WebRTC et relay afin qu’un rerender, une reconnexion ou une fin tardive ne rejoue aucun effet.",
                    "Les états attente, vérification, reprise, annulation, succès et blocage inconnu sont cohérents sur PC et Mobile avec textes localisés en anglais et français.",
                    "Windows 1.1.2 reste le rollback validé ; Windows 1.1.3 reste NotSigned sans éditeur revendiqué et conserve la politique Stable non signée officielle et bornée.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.2",
        released_on="2026-08-03",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "The updater now accepts future explicitly authorized unsigned Stable releases without a hardcoded future-version allowlist.",
            "Legacy stable_unsigned and stable_notsigned failures now explain that the installed updater is too old and provide a safe official bridge.",
            "Strict HTTPS, exact official endpoint, Windows x64 identity, versioned path, Content-Length, size, SHA-256, streaming, and full rehash protections remain mandatory.",
            "Windows 1.1.2 remains NotSigned with no publisher claim; explicit consent and truthful Unknown Publisher and SmartScreen disclosure are unchanged.",
            "Windows 1.1.1 and older Stable clients need a one-time Preview or verified manual in-place bridge before they gain the corrected updater policy.",
        ),
        summary_fr=(
            "L’updater accepte désormais les futures versions Stable non signées explicitement autorisées, sans allowlist codée en dur des versions futures.",
            "Les erreurs historiques stable_unsigned et stable_notsigned expliquent maintenant que l’updater installé est trop ancien et proposent un bridge officiel sûr.",
            "HTTPS strict, endpoint officiel exact, identité Windows x64, chemin versionné, Content-Length, taille, SHA-256, streaming et rehash complet restent obligatoires.",
            "Windows 1.1.2 reste NotSigned sans éditeur revendiqué ; le consentement explicite et la divulgation Éditeur inconnu et SmartScreen restent inchangés.",
            "Les clients Stable Windows 1.1.1 et antérieurs nécessitent un bridge unique via Preview ou une installation manuelle in-place vérifiée avant de bénéficier de la politique corrigée.",
        ),
        sections=(
            ReleaseSection(
                "Durable Stable compatibility",
                "Compatibilité Stable durable",
                (
                    "The temporary pre-WOR-220 policy now authorizes a newer unsigned Stable release only when it comes from the exact official Stable endpoint and matches OSIA Windows x64 on download.osia.space with its exact versioned installer path.",
                    "Authorization is no longer tied to a hardcoded list of future versions, so a corrected 1.1.2 client can evaluate a later explicitly authorized unsigned Stable release without reproducing this compatibility failure.",
                    "Disabling the isolated temporary policy restores fail-closed unsigned Stable behavior cleanly when recognized Authenticode signing arrives through WOR-220.",
                ),
                (
                    "La politique temporaire pré-WOR-220 n’autorise une nouvelle version Stable non signée que si elle provient de l’endpoint Stable officiel exact et correspond à OSIA Windows x64 sur download.osia.space avec son chemin d’installateur versionné exact.",
                    "L’autorisation n’est plus liée à une liste codée en dur des versions futures ; un client 1.1.2 corrigé peut donc évaluer une future Stable non signée explicitement autorisée sans reproduire cette incompatibilité.",
                    "La désactivation de cette politique temporaire isolée rétablit proprement le refus des Stable non signées lorsque WOR-220 apportera une signature Authenticode reconnue.",
                ),
            ),
            ReleaseSection(
                "Integrity and consent remain strict",
                "Intégrité et consentement toujours stricts",
                (
                    "The updater still requires strict HTTPS, the exact official Stable manifest endpoint, OSIA/windows/x86_64 identity, download.osia.space, and /windows/<version>/OSIA-Setup.exe without redirects or conflicting fields.",
                    "Content-Length and manifest size must match exactly, the streamed payload must match the exact SHA-256, and the completed installer is fully rehashed again before execution.",
                    "Installation still requires explicit user consent, uses shell=False with fixed arguments, and has no silent fallback for malformed manifests, alternate hosts, paths, sizes, or hashes.",
                ),
                (
                    "L’updater exige toujours HTTPS strict, l’endpoint de manifeste Stable officiel exact, l’identité OSIA/windows/x86_64, download.osia.space et /windows/<version>/OSIA-Setup.exe, sans redirection ni champs conflictuels.",
                    "Content-Length et la taille du manifeste doivent correspondre exactement, le flux doit avoir le SHA-256 exact et l’installateur terminé est entièrement rehashé avant exécution.",
                    "L’installation exige toujours un consentement explicite, utilise shell=False avec des arguments fixes et n’applique aucun fallback silencieux pour un manifeste, hôte, chemin, taille ou hash invalide.",
                ),
            ),
            ReleaseSection(
                "Truthful unsigned release and legacy bridge",
                "Version non signée transparente et bridge historique",
                (
                    "Windows 1.1.2 is intentionally distributed as NotSigned with no publisher claim; Windows may show Unknown Publisher or Microsoft Defender SmartScreen, and no Windows protection should be disabled.",
                    "The localized stable_unsigned and stable_notsigned diagnostics now explain that the installed updater is too old and direct users only to the official verified manual-update path.",
                    "Windows 1.1.1 and older Stable binaries cannot be repaired retroactively and do not automatically install 1.1.2 through Stable; use Preview from 1.1.1 where already supported, or install the verified official 1.1.2 installer in place once.",
                    "The in-place bridge preserves OSIA settings, conversations, personality, skills, and pairing; Windows 1.1.1 remains the validated rollback artifact.",
                ),
                (
                    "Windows 1.1.2 est volontairement distribué avec le statut NotSigned et sans éditeur revendiqué ; Windows peut afficher Éditeur inconnu ou Microsoft Defender SmartScreen, sans qu’aucune protection Windows doive être désactivée.",
                    "Les diagnostics localisés stable_unsigned et stable_notsigned expliquent désormais que l’updater installé est trop ancien et orientent uniquement vers le parcours officiel de mise à jour manuelle vérifiée.",
                    "Les binaires Stable Windows 1.1.1 et antérieurs ne peuvent pas être corrigés rétroactivement et n’installent pas automatiquement 1.1.2 via Stable ; utilisez Preview depuis 1.1.1 s’il est déjà pris en charge, ou installez une fois l’installateur officiel 1.1.2 vérifié in-place.",
                    "Le bridge in-place préserve réglages OSIA, conversations, personnalité, skills et association ; Windows 1.1.1 reste l’artefact de rollback validé.",
                ),
            ),
        ),
    ),
    ReleaseEntry(
        version="1.1.1",
        released_on="2026-08-03",
        public=True,
        changes_en=(),
        changes_fr=(),
        summary_en=(
            "Stable/Preview update preferences are now explicit, persistent, safely migrated, and isolated so Preview failures never block Stable.",
            "Skill activation now survives stale Mobile snapshots, navigation, editing, reconnects, and restarts while invalid skills remain fail-closed.",
            "Each turn keeps its chosen language across skills, tools, provider continuations, retries, finalization, Mobile projection, and TTS.",
            "Long or explicit chat requests can start one canonical Autonomous mission while simple, scheduled, ambiguous, or blocked work stays on its proper path.",
            "Chat-started missions inherit the exact active model and supported attachments; Windows gains animated switches and Android 1.1.0 mirrors the run accurately.",
        ),
        summary_fr=(
            "Les préférences Stable/Preview sont explicites, persistantes, migrées sûrement et isolées afin qu’un échec Preview ne bloque jamais Stable.",
            "L’activation des skills résiste aux anciens snapshots Mobile, à la navigation, à l’édition, aux reconnexions et redémarrages, avec repli sûr si invalide.",
            "Chaque tour conserve sa langue à travers skills, outils, continuations fournisseur, reprises, finalisation, projection Mobile et TTS.",
            "Une demande longue ou explicite du chat peut lancer une unique mission Autonomous ; les demandes simples, planifiées, ambiguës ou bloquées gardent leur voie.",
            "Les missions issues du chat héritent du modèle et des pièces jointes exacts ; Windows reçoit des interrupteurs animés et Android 1.1.0 reflète le run fidèlement.",
        ),
        sections=(
            ReleaseSection(
                "Stable and Preview updates",
                "Mises à jour Stable et Preview",
                (
                    "Settings now offers an explicit, confirmed opt-in to receive internal-preview builds in addition to Stable releases.",
                    "Existing internal-preview installations migrate to opted in, while Stable and new installations remain Stable-only by default.",
                    "Stable and Preview use independent caches and validators; a missing, paused, invalid, or offline Preview channel cannot block a valid Stable update.",
                    "Disabling Preview takes effect immediately without downgrading the installed build, and equal versions continue to prefer Stable.",
                ),
                (
                    "Les paramètres proposent désormais un opt-in explicite et confirmé pour recevoir les builds internal-preview en plus des versions Stable.",
                    "Les installations internal-preview existantes migrent vers l’opt-in, tandis que Stable et les nouvelles installations restent en Stable uniquement par défaut.",
                    "Stable et Preview utilisent des caches et validateurs indépendants ; un canal Preview absent, suspendu, invalide ou hors ligne ne bloque jamais une mise à jour Stable valide.",
                    "La désactivation de Preview est immédiate sans rétrograder la version installée, et une égalité de version continue de privilégier Stable.",
                ),
            ),
            ReleaseSection(
                "Skill activation reliability",
                "Fiabilité de l’activation des skills",
                (
                    "Skill enablement is persisted together with a canonical revision so a delayed Mobile snapshot can no longer switch a newly enabled skill back off.",
                    "Enabled skills remain enabled after refresh, navigation, content edits, reconnects, and restarts, and are injected into agent context exactly once.",
                    "Invalid or unavailable skills still remain disabled with a sanitized reason instead of reporting a false success.",
                ),
                (
                    "L’activation d’un skill est persistée avec une révision canonique afin qu’un snapshot Mobile retardé ne puisse plus le remettre immédiatement sur OFF.",
                    "Les skills actifs le restent après rafraîchissement, navigation, édition, reconnexion et redémarrage, et ne sont injectés qu’une fois dans le contexte agent.",
                    "Un skill invalide ou indisponible reste désactivé avec une raison sanitizée au lieu d’afficher un faux succès.",
                ),
            ),
            ReleaseSection(
                "Per-turn language continuity",
                "Continuité de langue par tour",
                (
                    "The language selected from the latest clear user message is restated at every initial and continuation provider boundary without changing stored conversation history.",
                    "Skills, multilingual tool results, provider-thread reuse, retry, replan, and resume can no longer silently replace the authoritative language mid-turn.",
                    "A bounded consistency check protects the canonical final response while preserving quotations, source values, code, URLs, tables, and explicit translation requests.",
                ),
                (
                    "La langue choisie depuis le dernier message utilisateur clair est réaffirmée à chaque frontière fournisseur initiale ou de continuation sans modifier l’historique stocké.",
                    "Skills, résultats d’outils multilingues, réutilisation de thread fournisseur, retry, replan et resume ne peuvent plus remplacer silencieusement la langue autoritaire en cours de tour.",
                    "Un contrôle borné protège la réponse finale canonique tout en préservant citations, valeurs sources, code, URL, tableaux et demandes explicites de traduction.",
                ),
            ),
            ReleaseSection(
                "Chat to Autonomous routing",
                "Routage Chat vers Autonomous",
                (
                    "Clearly long, sustained, multi-step work can now be handed from Chat to the existing canonical Autonomous engine automatically, and explicit requests can select the same path.",
                    "Simple questions and quick tools stay in Chat, future or recurring requests stay in Scheduled Tasks, and ambiguous requests ask for clarification.",
                    "A successful backend start is required before confirmation, with one mission per message, no duplicate normal-chat execution, and existing permissions, idempotency, and single-runner guards preserved.",
                    "Pause, resume, abort, checkpoint, restart, and reconnect continue through the existing Autonomous state rather than a parallel store or orchestrator.",
                ),
                (
                    "Les travaux clairement longs, soutenus et multi-étapes peuvent être confiés automatiquement depuis Chat au moteur Autonomous canonique existant ; une demande explicite emprunte la même voie.",
                    "Les questions simples et outils rapides restent dans Chat, les demandes futures ou récurrentes restent dans Scheduled Tasks et les demandes ambiguës déclenchent une clarification.",
                    "Le démarrage backend doit réussir avant confirmation, avec une mission au plus par message, aucune double exécution Chat et le maintien des permissions, de l’idempotence et du verrou single-runner.",
                    "Pause, reprise, abandon, checkpoint, redémarrage et reconnexion continuent via l’état Autonomous existant, sans store ni orchestrateur parallèle.",
                ),
            ),
            ReleaseSection(
                "Exact model and attachment handoff",
                "Transmission exacte du modèle et des pièces jointes",
                (
                    "A mission started from Chat snapshots the exact effective provider and model from that turn and keeps the pair through planner, runner, verifier, retry, replan, reconnect, and resume.",
                    "Manual Autonomous launches keep their independent selector, and chat handoff never overwrites the global Autonomous preference.",
                    "Unavailable or incompatible chat models and genuine runner transport failures now produce distinct, localized diagnostics without silent fallback.",
                    "Supported opaque PNG, PDF, and DOCX references retain their permission epoch and remain readable after the handoff without copying unrelated private context.",
                ),
                (
                    "Une mission lancée depuis Chat capture le fournisseur et le modèle effectifs exacts du tour et conserve cette paire dans planner, runner, verifier, retry, replan, reconnexion et resume.",
                    "Les lancements Autonomous manuels gardent leur sélecteur indépendant et le handoff du chat n’écrase jamais la préférence Autonomous globale.",
                    "Un modèle de chat indisponible ou incompatible et une véritable erreur de transport runner produisent désormais des diagnostics distincts et localisés, sans fallback silencieux.",
                    "Les références opaques PNG, PDF et DOCX prises en charge conservent leur epoch d’autorisation et restent lisibles après le handoff sans copier de contexte privé inutile.",
                ),
            ),
            ReleaseSection(
                "Windows controls and Android companion",
                "Contrôles Windows et compagnon Android",
                (
                    "Windows binary settings now share one accessible sliding-switch component with a 180 ms animation, keyboard operation, clear focus, disabled state, light and dark themes, accent palettes, high-DPI support, and reduced motion.",
                    "Existing business bindings remain unchanged for permissions, Full Access, skills, providers, voice, Stable/Preview, Autonomous, Scheduled Tasks, and dynamically generated settings.",
                    "Android 1.1.0 rejects stale skill revisions, mirrors the inherited Autonomous run model, and hides only the non-actionable browser snapshot refresh card while preserving useful browser progress.",
                    "The protected Mobile WebSocket service and public message protocol remain unchanged.",
                ),
                (
                    "Les réglages binaires Windows partagent désormais un interrupteur coulissant accessible avec animation de 180 ms, clavier, focus clair, état disabled, thèmes clair/sombre, palettes d’accent, haute résolution et animations réduites.",
                    "Les bindings métier restent inchangés pour permissions, Full Access, skills, fournisseurs, voix, Stable/Preview, Autonomous, Scheduled Tasks et réglages générés dynamiquement.",
                    "Android 1.1.0 rejette les anciennes révisions de skills, reflète le modèle hérité du run Autonomous et masque uniquement la carte inutile de rafraîchissement du snapshot navigateur en conservant les progrès utiles.",
                    "Le service WebSocket Mobile protégé et le protocole public de messages restent inchangés.",
                ),
            ),
            ReleaseSection(
                "Distribution and known limits",
                "Distribution et limites connues",
                (
                    "Windows 1.1.1 is distributed without an Authenticode signature, so Windows may show Unknown publisher or a Microsoft Defender SmartScreen warning.",
                    "Download only from the official OSIA HTTPS domain, verify the published SHA-256, and use More info then Run anyway only when that verified official download is trusted; do not disable Windows protections.",
                    "Autonomous handoff remains subject to the active model’s real compatibility, configured access, permissions, and the single active-run limit; it never silently falls back or elevates privileges.",
                    "Google Play availability for Android 1.1.0 is reported separately on osia.space and must not be inferred from the Windows release.",
                ),
                (
                    "Windows 1.1.1 est distribué sans signature Authenticode ; Windows peut donc afficher Éditeur inconnu ou un avertissement Microsoft Defender SmartScreen.",
                    "Téléchargez uniquement depuis le domaine HTTPS officiel OSIA, vérifiez le SHA-256 publié et utilisez Informations complémentaires puis Exécuter quand même seulement si ce téléchargement officiel vérifié est digne de confiance ; ne désactivez aucune protection Windows.",
                    "Le handoff Autonomous reste soumis à la compatibilité réelle du modèle actif, aux accès configurés, aux permissions et à la limite d’un run actif ; il n’effectue aucun fallback silencieux ni élévation de privilèges.",
                    "La disponibilité Google Play d’Android 1.1.0 est indiquée séparément sur osia.space et ne doit pas être déduite de la release Windows.",
                ),
            ),
        ),
    ),
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
                "Unified Chat execution",
                "Exécution Unified Chat",
                (
                    "The Chat model picker shows only providers and models actually configured on the PC, including compatible ChatGPT Account models.",
                    "Plans and runs preserve typed failure diagnostics, provider identity, progress, resumable state, and safe document artifacts.",
                    "Scoped authorization limits what each run may do and keeps destructive or out-of-scope work behind the canonical permission boundary.",
                    "Resource checks and progress-aware loop detection stop unsafe or genuinely stalled work without suppressing legitimate long-running progress.",
                ),
                (
                    "Le sélecteur de modèles Chat n’affiche que les fournisseurs et modèles réellement configurés sur le PC, dont les modèles compatibles Compte ChatGPT.",
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
    sorted(
        _RELEASES,
        key=lambda entry: parse_product_version(HISTORICAL_DISPLAY_VERSIONS.get(entry.version, entry.version)),
        reverse=True,
    )
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
    return display_historical_text("\n".join(lines).strip()) + "\n"


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

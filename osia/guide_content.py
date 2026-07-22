"""Offline, bilingual content model for the integrated OSIA learning guide."""

from __future__ import annotations

from dataclasses import dataclass, replace


GUIDE_CONTENT_SCHEMA_VERSION = 3
CATEGORY_ESSENTIAL = "getting-started"
CATEGORY_MASTERY = "master-osia"
CATEGORY_ADVANCED = "osia-advanced"


Localized = dict[str, str]


@dataclass(frozen=True)
class GuideCategory:
    id: str
    semantic_id: str
    order: int
    title: Localized
    summary: Localized


@dataclass(frozen=True)
class GuideLesson:
    id: str
    category: str
    order: int
    title: Localized
    intro: Localized
    paragraphs: tuple[Localized, ...]
    example_title: Localized
    example: Localized
    remember: Localized | None = None
    asset: str | None = None
    caption: Localized | None = None
    alt_text: Localized | None = None
    subject_id: str | None = None
    glossary_ids: tuple[str, ...] = ()


def localized(value: Localized | None, language: str) -> str:
    """Resolve guide copy in ``language``, falling back to English."""

    if not value:
        return ""
    return value.get(language) or value.get("en") or ""


CATEGORIES = (
    GuideCategory(
        id=CATEGORY_ESSENTIAL,
        semantic_id="essential",
        order=1,
        title={"en": "Getting started", "fr": "Démarrage essentiel"},
        summary={
            "en": "The essentials for a confident first conversation, with no technical knowledge required.",
            "fr": "L’essentiel pour commencer avec confiance, sans connaissances techniques.",
        },
    ),
    GuideCategory(
        id=CATEGORY_MASTERY,
        semantic_id="mastery",
        order=2,
        title={"en": "Master OSIA", "fr": "Maîtriser OSIA"},
        summary={
            "en": "Every OSIA feature, explained clearly with practical examples.",
            "fr": "Toutes les fonctions d’OSIA, expliquées clairement avec des exemples.",
        },
    ),
    GuideCategory(
        id=CATEGORY_ADVANCED,
        semantic_id="advanced",
        order=3,
        title={"en": "OSIA: Advanced Guide", "fr": "OSIA : Guide avancé"},
        summary={
            "en": "Architecture, protocols, development, security, and project continuation.",
            "fr": "Architecture, protocoles, développement, sécurité et reprise du projet.",
        },
    ),
)


_ORIGINAL_LESSONS = (
    GuideLesson(
        id="essential.what-is-osia",
        category=CATEGORY_ESSENTIAL,
        order=1,
        title={"en": "What OSIA is", "fr": "Qu’est-ce qu’OSIA ?"},
        intro={
            "en": "OSIA is an assistant that runs primarily on your computer. You can talk to it from the PC interface or from OSIA Mobile.",
            "fr": "OSIA est un assistant qui fonctionne principalement sur votre ordinateur. Vous pouvez lui parler depuis l’interface PC ou OSIA Mobile.",
        },
        paragraphs=(
            {
                "en": "It can answer questions, explain information and use the tools that are available on your computer. You remain in control of how it obtains AI responses and which actions you request.",
                "fr": "Il peut répondre à des questions, expliquer des informations et utiliser les outils disponibles sur votre ordinateur. Vous gardez le contrôle sur la façon dont il obtient ses réponses IA et sur les actions demandées.",
            },
            {
                "en": "OSIA is capable, but it is not magic: the PC must be running, the required connection must work, and an action must be supported by an installed tool. Review important results before relying on them.",
                "fr": "OSIA est puissant, mais pas magique : le PC doit fonctionner, la connexion nécessaire doit être disponible et l’action doit être prise en charge par un outil installé. Vérifiez les résultats importants.",
            },
        ),
        example_title={"en": "Try requests like", "fr": "Exemples de demandes"},
        example={
            "en": "• “Summarize this document.”\n• “Help me plan my week.”\n• “Explain this error.”\n• “Use my computer tools to complete this task.”",
            "fr": "• « Résume ce document. »\n• « Aide-moi à organiser ma semaine. »\n• « Explique cette erreur. »\n• « Utilise les outils de mon ordinateur pour accomplir cette tâche. »",
        },
        remember={
            "en": "OSIA works from your PC and phone, but supported tools, connections and your approval still set its practical limits.",
            "fr": "OSIA fonctionne depuis le PC et le téléphone, mais ses limites dépendent des outils, des connexions et de vos choix.",
        },
    ),
    GuideLesson(
        id="essential.answer-methods",
        category=CATEGORY_ESSENTIAL,
        order=2,
        title={"en": "How OSIA can answer", "fr": "Comment OSIA peut répondre"},
        intro={
            "en": "OSIA needs one AI connection method before it can prepare useful answers.",
            "fr": "OSIA a besoin d’une méthode de connexion à une IA pour préparer des réponses utiles.",
        },
        paragraphs=(
            {
                "en": "The first path is a ChatGPT Account connection. OSIA uses its managed account integration and keeps that selection separate from API providers.",
                "fr": "La première possibilité est de connecter un compte ChatGPT. OSIA utilise son intégration de compte gérée et conserve ce choix séparé des fournisseurs API.",
            },
            {
                "en": "The second path is an API key from a provider such as Anthropic, OpenAI, Google, DeepSeek, xAI, Groq or Mistral. A local Ollama model can also be selected when it is installed.",
                "fr": "La seconde possibilité est une clé API d’un fournisseur comme Anthropic, OpenAI, Google, DeepSeek, xAI, Groq ou Mistral. Un modèle Ollama local peut aussi être sélectionné s’il est installé.",
            },
            {
                "en": "You do not normally need both paths. Availability, price, limits and data handling depend on the account, provider and model you choose; OSIA does not make them free or unlimited.",
                "fr": "Vous n’avez généralement pas besoin des deux. La disponibilité, le prix, les limites et le traitement des données dépendent du compte, du fournisseur et du modèle choisis ; OSIA ne les rend ni gratuits ni illimités.",
            },
        ),
        example_title={"en": "Simple decision", "fr": "Choix simple"},
        example={
            "en": "If you want to use your connected ChatGPT account, choose that route. If you received a provider API key, save it and select one of that provider’s models.",
            "fr": "Pour utiliser votre compte ChatGPT connecté, choisissez cette méthode. Si vous avez reçu une clé API, enregistrez-la puis sélectionnez un modèle de ce fournisseur.",
        },
        remember={
            "en": "Configure one working AI method first. You can add or change methods later in Settings → Models.",
            "fr": "Configurez d’abord une méthode IA fonctionnelle. Vous pourrez la modifier plus tard dans Réglages → Modèles.",
        },
    ),
    GuideLesson(
        id="essential.chatgpt-account",
        category=CATEGORY_ESSENTIAL,
        order=3,
        title={"en": "Connect a ChatGPT Account", "fr": "Connecter un compte ChatGPT"},
        intro={
            "en": "Open Settings → Models, then choose ChatGPT Account in the provider selector.",
            "fr": "Ouvrez Réglages → Modèles, puis choisissez Compte ChatGPT dans la liste des fournisseurs.",
        },
        paragraphs=(
            {
                "en": "Select Connect and follow the displayed sign-in instructions. Authentication happens through OSIA’s isolated managed account flow; the guide never opens it for you.",
                "fr": "Sélectionnez Connecter et suivez les instructions affichées. L’authentification utilise le flux de compte géré et isolé d’OSIA ; le guide ne l’ouvre jamais à votre place.",
            },
            {
                "en": "A check mark and Connected status confirm that the account is ready. The page may also show the selected account model and sanitized usage information.",
                "fr": "Une coche et l’état Connecté confirment que le compte est prêt. La page peut aussi afficher le modèle du compte et des informations d’utilisation assainies.",
            },
            {
                "en": "If connection fails, use Retry after checking the browser flow and network. Disconnect removes the managed OSIA account session; it does not delete the ChatGPT account itself.",
                "fr": "En cas d’échec, utilisez Réessayer après avoir vérifié le navigateur et le réseau. Déconnecter retire la session gérée d’OSIA ; cela ne supprime pas le compte ChatGPT.",
            },
        ),
        example_title={"en": "What success looks like", "fr": "À quoi ressemble la réussite"},
        example={
            "en": "Provider: ChatGPT Account\nStatus: ✓ Connected\nNext: choose an available account model in the Models page.",
            "fr": "Fournisseur : Compte ChatGPT\nÉtat : ✓ Connecté\nEnsuite : choisissez un modèle de compte disponible dans la page Modèles.",
        },
        remember={
            "en": "Never share sign-in codes or account session data. A connected status inside OSIA is the confirmation you need.",
            "fr": "Ne partagez jamais les codes de connexion ni les données de session. L’état Connecté dans OSIA suffit comme confirmation.",
        },
        asset="onboarding/settings-chatgpt-account.png",
        caption={
            "en": "Sanitized OSIA Models page with the ChatGPT Account provider selected.",
            "fr": "Page Modèles d’OSIA assainie avec le fournisseur Compte ChatGPT sélectionné.",
        },
        alt_text={
            "en": "OSIA Settings Models category. ChatGPT Account is selected; the account card shows a disconnected demo state and connection controls, with no personal account data.",
            "fr": "Catégorie Modèles des réglages OSIA. Compte ChatGPT est sélectionné ; la carte affiche un état de démonstration déconnecté et les commandes de connexion, sans donnée personnelle.",
        },
    ),
    GuideLesson(
        id="essential.api-key",
        category=CATEGORY_ESSENTIAL,
        order=4,
        title={"en": "Add an API key", "fr": "Ajouter une clé API"},
        intro={
            "en": "An API key is a private access code supplied by an AI provider. Treat it like a password.",
            "fr": "Une clé API est un code d’accès privé fourni par un fournisseur d’IA. Traitez-la comme un mot de passe.",
        },
        paragraphs=(
            {
                "en": "In Settings → Models, choose the provider, paste its key into that provider’s protected field, then save it. OSIA stores provider keys outside normal settings data.",
                "fr": "Dans Réglages → Modèles, choisissez le fournisseur, collez sa clé dans le champ protégé, puis enregistrez-la. OSIA conserve les clés hors des réglages ordinaires.",
            },
            {
                "en": "After saving, select a model belonging to that provider. A provider and a model are different: the provider operates the service; the model is the particular AI you ask to answer.",
                "fr": "Après l’enregistrement, sélectionnez un modèle de ce fournisseur. Le fournisseur exploite le service ; le modèle est l’IA particulière à laquelle OSIA demande de répondre.",
            },
            {
                "en": "If it does not work, verify that the key belongs to the selected provider, has no extra spaces, is active, has the required access or credit, and that the network is available.",
                "fr": "Si cela ne fonctionne pas, vérifiez que la clé appartient au fournisseur choisi, ne contient pas d’espace en trop, est active, dispose de l’accès ou du crédit nécessaire et que le réseau fonctionne.",
            },
        ),
        example_title={"en": "Safe example", "fr": "Exemple sûr"},
        example={
            "en": "The field should look masked after saving: ••••••••••••\nNever paste a real key into a chat, screenshot, email or support message.",
            "fr": "Après enregistrement, le champ doit être masqué : ••••••••••••\nNe collez jamais une vraie clé dans une discussion, une capture, un e-mail ou un message d’assistance.",
        },
        remember={
            "en": "Save the key under the correct provider, then select that provider’s model. Keep the key private.",
            "fr": "Enregistrez la clé sous le bon fournisseur, puis choisissez son modèle. Gardez la clé privée.",
        },
        asset="onboarding/settings-api-provider.png",
        caption={
            "en": "Sanitized Models page showing provider selection and a fake masked key field.",
            "fr": "Page Modèles assainie montrant le choix du fournisseur et un faux champ de clé masqué.",
        },
        alt_text={
            "en": "OSIA Settings Models category with an API provider selected. The key control contains only bullet masking and the model controls contain public product names.",
            "fr": "Catégorie Modèles des réglages OSIA avec un fournisseur API sélectionné. Le champ de clé ne contient que des puces et les modèles portent des noms publics.",
        },
    ),
    GuideLesson(
        id="essential.mobile-pairing",
        category=CATEGORY_ESSENTIAL,
        order=5,
        title={"en": "Connect OSIA Mobile", "fr": "Connecter OSIA Mobile"},
        intro={
            "en": "The pairing token lets your phone prove that it belongs to your OSIA PC. Keep it private.",
            "fr": "Le jeton d’association permet au téléphone de prouver qu’il appartient à votre PC OSIA. Gardez-le privé.",
        },
        paragraphs=(
            {
                "en": "Install or open OSIA Mobile. On the PC, open Settings → Connection / Mobile and reveal the pairing token only when you are ready to enter it on your phone.",
                "fr": "Installez ou ouvrez OSIA Mobile. Sur le PC, ouvrez Réglages → Connexion / Mobile et n’affichez le jeton que lorsque vous êtes prêt à le saisir sur le téléphone.",
            },
            {
                "en": "Enter the token exactly, then complete the pairing screen. The phone uses token-derived proofs and encrypted handshakes to find and authenticate the PC; the raw token is not published to the public relay.",
                "fr": "Saisissez exactement le jeton, puis terminez l’association. Le téléphone utilise des preuves dérivées du jeton et des échanges chiffrés pour trouver et authentifier le PC ; le jeton brut n’est pas publié au relais public.",
            },
            {
                "en": "A connected badge in the mobile app confirms success. Regenerating the PC token invalidates the old one, so every phone must pair again.",
                "fr": "Un badge Connecté dans l’application mobile confirme la réussite. Régénérer le jeton PC invalide l’ancien ; chaque téléphone doit alors être associé de nouveau.",
            },
        ),
        example_title={"en": "Pairing procedure", "fr": "Procédure d’association"},
        example={
            "en": "1. Open Connection / Mobile on the PC.\n2. Reveal and enter the token on the phone.\n3. Complete pairing and wait for Connected.\n4. Hide the token again and never include it in a screenshot.",
            "fr": "1. Ouvrez Connexion / Mobile sur le PC.\n2. Affichez puis saisissez le jeton sur le téléphone.\n3. Terminez l’association et attendez l’état Connecté.\n4. Masquez de nouveau le jeton et ne l’incluez jamais dans une capture.",
        },
        remember={
            "en": "The token is the shared pairing secret. The PC must be running and reachable for the phone to use OSIA.",
            "fr": "Le jeton est le secret partagé d’association. Le PC doit être allumé et joignable pour utiliser OSIA depuis le téléphone.",
        },
        asset="onboarding/settings-mobile-pairing.png",
        caption={
            "en": "Sanitized Connection / Mobile page with a fully masked demonstration token.",
            "fr": "Page Connexion / Mobile assainie avec un jeton de démonstration entièrement masqué.",
        },
        alt_text={
            "en": "OSIA Settings Connection / Mobile category. The pairing token is completely masked and no PC name, IP address, device identity or certificate is visible.",
            "fr": "Catégorie Connexion / Mobile des réglages OSIA. Le jeton est entièrement masqué et aucun nom de PC, adresse IP, appareil ou certificat n’est visible.",
        },
    ),
    GuideLesson(
        id="essential.menu-tour",
        category=CATEGORY_ESSENTIAL,
        order=6,
        title={"en": "Understand the OSIA menu", "fr": "Comprendre le menu OSIA"},
        intro={
            "en": "Settings is organized into ten real categories. Each one has a specific purpose.",
            "fr": "Les réglages sont organisés en dix catégories réelles. Chacune a un rôle précis.",
        },
        paragraphs=(
            {
                "en": "General — language, appearance, capsule, startup, Restart/Close OSIA and this guide.\nModels — ChatGPT Account, providers, API keys, models and fallback hierarchy.\nVoice — spoken output (TTS) and microphone transcription (STT).\nWake word — listening for “OSIA” and its optional greeting.\nMemory — review and control long-term local memory.",
                "fr": "Général — langue, apparence, capsule, démarrage, Redémarrer/Fermer OSIA et ce guide.\nModèles — Compte ChatGPT, fournisseurs, clés API, modèles et hiérarchie de secours.\nVoix — sortie parlée (TTS) et transcription du micro (STT).\nMot d’activation — écoute de « OSIA » et message d’accueil facultatif.\nMémoire — consultation et contrôle de la mémoire locale durable.",
            },
            {
                "en": "MCP / Connectors — optional external tool connections and write-safety choices.\nConnection / Mobile — phone pairing and owned server startup.\nAdvanced — scheduled tasks and PC health monitoring.\nUpdates — versions, changes and update status.\nLegal — versions, privacy and legal information.",
                "fr": "MCP / Connecteurs — connexions facultatives à des outils externes et sécurité des écritures.\nConnexion / Mobile — association du téléphone et démarrage du serveur.\nAvancé — tâches planifiées et surveillance du PC.\nMises à jour — versions, changements et état des mises à jour.\nLégal — versions, confidentialité et informations légales.",
            },
            {
                "en": "The bottom navigation also opens Conversations, Models, Settings, OSIA personality/skills, and Autonomous mode. Features that are disabled or unavailable remain visibly unavailable; the guide does not activate them.",
                "fr": "La navigation inférieure ouvre aussi Conversations, Modèles, Réglages, la personnalité/les compétences OSIA et le mode Autonome. Les fonctions désactivées ou indisponibles le restent ; le guide ne les active pas.",
            },
        ),
        example_title={"en": "Where should I go?", "fr": "Où dois-je aller ?"},
        example={
            "en": "To change the speaking voice, open Voice. To pair a phone, open Connection / Mobile. To review what OSIA remembers, open Memory. To reopen this guide, open General.",
            "fr": "Pour changer la voix, ouvrez Voix. Pour associer un téléphone, ouvrez Connexion / Mobile. Pour revoir ce qu’OSIA mémorise, ouvrez Mémoire. Pour rouvrir ce guide, ouvrez Général.",
        },
        remember={
            "en": "Settings changes OSIA’s behavior; the guide only explains where controls are and never changes them.",
            "fr": "Les réglages modifient le comportement d’OSIA ; le guide explique les commandes sans jamais les modifier.",
        },
        asset="onboarding/settings-menu.png",
        caption={
            "en": "OSIA Settings with all current category names visible in the left menu.",
            "fr": "Réglages OSIA avec toutes les catégories actuelles visibles dans le menu gauche.",
        },
        alt_text={
            "en": "OSIA Settings window. The left column lists General, Models, Voice, Wake word, Memory, MCP / Connectors, Connection / Mobile, Advanced, Updates and Legal.",
            "fr": "Fenêtre Réglages d’OSIA. La colonne gauche liste Général, Modèles, Voix, Mot d’activation, Mémoire, MCP / Connecteurs, Connexion / Mobile, Avancé, Mises à jour et Légal.",
        },
    ),
    GuideLesson(
        id="essential.first-requests",
        category=CATEGORY_ESSENTIAL,
        order=7,
        title={"en": "Your first useful requests", "fr": "Vos premières demandes utiles"},
        intro={
            "en": "A clear request tells OSIA the goal, the useful context and the result you expect.",
            "fr": "Une demande claire indique à OSIA le but, le contexte utile et le résultat attendu.",
        },
        paragraphs=(
            {
                "en": "Ask a question: “Explain compound interest in simple terms.” Understand a file or error: “Read this error and give me the three most likely causes.” Organize work: “Turn these notes into a prioritized checklist for Friday.”",
                "fr": "Posez une question : « Explique les intérêts composés simplement. » Comprenez un fichier ou une erreur : « Lis cette erreur et donne-moi les trois causes les plus probables. » Organisez le travail : « Transforme ces notes en liste priorisée pour vendredi. »",
            },
            {
                "en": "Use voice: say the complete goal after OSIA starts listening. Continue from the phone: open the same conversation and add the missing context. Ask for a supported action: name the application, item or destination precisely.",
                "fr": "Utilisez la voix : énoncez le but complet quand OSIA écoute. Continuez depuis le téléphone : ouvrez la même conversation et ajoutez le contexte manquant. Pour une action prise en charge, nommez précisément l’application, l’élément ou la destination.",
            },
            {
                "en": "For important actions, ask OSIA to explain the plan or show the proposed result first. Check names, dates, recipients and files before approving anything consequential.",
                "fr": "Pour les actions importantes, demandez d’abord à OSIA d’expliquer le plan ou de montrer le résultat prévu. Vérifiez noms, dates, destinataires et fichiers avant toute validation importante.",
            },
        ),
        example_title={"en": "A strong request", "fr": "Une bonne demande"},
        example={
            "en": "Goal: prepare tomorrow’s work.\nContext: these meeting notes and a 90-minute time limit.\nExpected result: a numbered plan with the three most important tasks first.",
            "fr": "But : préparer le travail de demain.\nContexte : ces notes de réunion et une limite de 90 minutes.\nRésultat attendu : un plan numéroté avec les trois tâches les plus importantes en premier.",
        },
        remember={
            "en": "Goal + context + expected result is the simplest recipe for a useful request.",
            "fr": "But + contexte + résultat attendu : la formule la plus simple pour une demande utile.",
        },
    ),
    GuideLesson(
        id="essential.ready",
        category=CATEGORY_ESSENTIAL,
        order=8,
        title={"en": "You are ready", "fr": "Vous êtes prêt"},
        intro={
            "en": "You now know the minimum needed to begin using OSIA confidently.",
            "fr": "Vous connaissez maintenant le minimum nécessaire pour utiliser OSIA avec confiance.",
        },
        paragraphs=(
            {
                "en": "Make sure one AI method works: either a connected ChatGPT Account, a saved provider API key and selected model, or an installed local Ollama model.",
                "fr": "Vérifiez qu’une méthode IA fonctionne : compte ChatGPT connecté, clé API enregistrée avec un modèle choisi, ou modèle Ollama local installé.",
            },
            {
                "en": "Phone pairing is optional. On the PC you can type; with microphone access and working voice settings you can also speak. Every choice can be changed later in Settings.",
                "fr": "L’association du téléphone est facultative. Sur le PC, vous pouvez écrire ; avec l’autorisation du micro et des réglages vocaux fonctionnels, vous pouvez aussi parler. Tous les choix restent modifiables dans les réglages.",
            },
            {
                "en": "The Master OSIA category remains available offline whenever you want deeper explanations of connections, voice, providers, models, memory, skills, automation and safety.",
                "fr": "La catégorie Maîtriser OSIA reste disponible hors ligne pour approfondir les connexions, la voix, les fournisseurs, les modèles, la mémoire, les compétences, l’automatisation et la sécurité.",
            },
        ),
        example_title={"en": "Ready checklist", "fr": "Liste de départ"},
        example={
            "en": "✓ One AI method configured\n○ Phone paired if wanted\n✓ Type a first request, or speak after enabling voice\n✓ Return to Settings whenever you want to adjust OSIA",
            "fr": "✓ Une méthode IA configurée\n○ Téléphone associé si souhaité\n✓ Écrire une première demande, ou parler après activation de la voix\n✓ Revenir aux réglages pour ajuster OSIA à tout moment",
        },
        remember={
            "en": "Finish closes the first-run guide. It will not be forced again; you can reopen the complete guide from Settings → General.",
            "fr": "Terminer ferme le guide initial. Il ne sera plus imposé ; vous pourrez rouvrir le guide complet depuis Réglages → Général.",
        },
    ),
    GuideLesson(
        id="advanced.pc-mobile-connection",
        category=CATEGORY_ADVANCED,
        order=1,
        title={"en": "How PC and phone connect", "fr": "Connexion entre le PC et le téléphone"},
        intro={
            "en": "Pairing creates trust; transports are simply the different roads the phone can use to reach the same OSIA server on your PC.",
            "fr": "L’association crée la confiance ; les transports sont simplement les différentes routes permettant au téléphone d’atteindre le même serveur OSIA sur votre PC.",
        },
        paragraphs=(
            {
                "en": "The token is the shared pairing secret. From it, PC and phone derive proofs and fresh encryption keys. The public relay routes encrypted data and does not receive the raw token.",
                "fr": "Le jeton est le secret d’association partagé. Le PC et le téléphone en dérivent des preuves et de nouvelles clés de chiffrement. Le relais public achemine des données chiffrées sans recevoir le jeton brut.",
            },
            {
                "en": "Connection order is direct-first: (1) try an already provisioned private-LAN WSS connection; (2) open the end-to-end encrypted relay in standby; (3) retry newly provisioned LAN; (4) try data-only WebRTC/P2P with encrypted signaling through the relay; (5) explicitly commit the encrypted relay fallback.",
                "fr": "L’ordre privilégie le direct : (1) essayer une connexion WSS déjà configurée sur le réseau local privé ; (2) ouvrir le relais chiffré de bout en bout en attente ; (3) réessayer le réseau local nouvellement configuré ; (4) essayer WebRTC/P2P pour les données, avec signalisation chiffrée par le relais ; (5) activer explicitement le relais chiffré en secours.",
            },
            {
                "en": "Only one road may carry application traffic at a time. After sleep or a network change, reconnect attempts are coalesced. OSIA does not switch roads during an active request because replaying part of a response could duplicate work.",
                "fr": "Une seule route transporte les données de l’application à la fois. Après une veille ou un changement de réseau, les reconnexions sont regroupées. OSIA ne change pas de route pendant une demande active afin d’éviter de dupliquer une partie du travail.",
            },
            {
                "en": "The older direct host/rendezvous WebSocket route is a gated legacy fallback and is disabled by default. If the PC is off, asleep without a working server, disconnected, or unable to reach any usable transport, the phone cannot run OSIA tasks.",
                "fr": "L’ancienne route WebSocket directe par hôte/rendez-vous est un secours historique conditionnel, désactivé par défaut. Si le PC est éteint, en veille sans serveur actif, déconnecté ou sans transport utilisable, le téléphone ne peut pas exécuter de tâches OSIA.",
            },
        ),
        example_title={"en": "Worked example", "fr": "Exemple concret"},
        example={
            "en": "At home, the phone may connect over the authenticated private LAN. Away from home, WebRTC may establish a direct path; if it cannot, the end-to-end encrypted relay carries the conversation. The OSIA task still runs on the PC.",
            "fr": "À la maison, le téléphone peut utiliser le réseau local privé authentifié. À distance, WebRTC peut créer une route directe ; sinon, le relais chiffré de bout en bout transporte la conversation. La tâche OSIA s’exécute toujours sur le PC.",
        },
        remember={
            "en": "LAN → relay standby → provisioned LAN retry → WebRTC → committed relay. One authenticated application transport owns the bridge at a time.",
            "fr": "LAN → relais en attente → nouvel essai LAN → WebRTC → relais activé. Un seul transport authentifié contrôle la connexion à la fois.",
        },
        asset="onboarding/settings-mobile-pairing.png",
        caption={
            "en": "The sanitized PC pairing page; connection selection happens automatically after pairing.",
            "fr": "Page d’association PC assainie ; le choix de la connexion est automatique après l’association.",
        },
        alt_text={
            "en": "OSIA Connection / Mobile settings with the secret token hidden. This page establishes pairing; LAN, WebRTC and relay selection occur automatically in the connection services.",
            "fr": "Réglages Connexion / Mobile avec le jeton secret masqué. Cette page établit l’association ; le choix entre LAN, WebRTC et relais est automatique.",
        },
    ),
    GuideLesson(
        id="advanced.voice-tts-stt",
        category=CATEGORY_ADVANCED,
        order=2,
        title={"en": "TTS and STT", "fr": "TTS et STT"},
        intro={
            "en": "TTS means OSIA turns text into spoken audio. STT means OSIA turns your microphone audio into text.",
            "fr": "TTS signifie qu’OSIA transforme du texte en voix. STT signifie qu’OSIA transforme le son du microphone en texte.",
        },
        paragraphs=(
            {
                "en": "In Settings → Voice, TTS can use local Kokoro (offline by default), OpenAI, or Google. Cloud voices require the matching provider key and a network; if cloud synthesis fails, normal PC speech falls back to local Kokoro.",
                "fr": "Dans Réglages → Voix, le TTS peut utiliser Kokoro local (hors ligne par défaut), OpenAI ou Google. Les voix cloud demandent la clé correspondante et un réseau ; en cas d’échec, la parole normale du PC revient à Kokoro local.",
            },
            {
                "en": "STT can use local faster-whisper or hosted Groq Whisper. Local offers installed Small/Medium models; Groq offers its hosted Whisper models and requires a Groq key. You can select local, Groq or no fallback when the primary transcription fails.",
                "fr": "Le STT peut utiliser faster-whisper local ou Whisper hébergé par Groq. Le mode local propose les modèles Small/Medium installés ; Groq propose ses modèles hébergés et demande une clé Groq. Le secours peut être local, Groq ou aucun.",
            },
            {
                "en": "Allow microphone access in the operating system. If OSIA hears nothing, check the default input device and privacy permission. If words are wrong, reduce background noise, speak closer to the microphone, or try the other STT engine/model.",
                "fr": "Autorisez le microphone dans le système. Si OSIA n’entend rien, vérifiez le périphérique d’entrée et l’autorisation de confidentialité. Si la transcription est mauvaise, réduisez le bruit, rapprochez-vous du micro ou essayez l’autre moteur/modèle STT.",
            },
            {
                "en": "Local TTS/STT keeps synthesis or transcription on the PC after models are installed. Cloud choices send the required text or audio to that provider under its service terms.",
                "fr": "Le TTS/STT local conserve la synthèse ou la transcription sur le PC une fois les modèles installés. Les choix cloud envoient le texte ou l’audio nécessaire au fournisseur selon ses conditions.",
            },
        ),
        example_title={"en": "Troubleshooting path", "fr": "Dépannage simple"},
        example={
            "en": "No transcription → check microphone permission → check the selected STT model is installed/keyed → try the configured fallback.\nNo spoken answer → check volume/output → test local Kokoro → verify the selected cloud key if applicable.",
            "fr": "Pas de transcription → vérifier l’autorisation du micro → vérifier le modèle STT ou la clé → essayer le secours configuré.\nPas de réponse parlée → vérifier le volume et la sortie → tester Kokoro local → vérifier la clé cloud choisie si nécessaire.",
        },
        remember={
            "en": "TTS speaks; STT listens. Local choices favor offline processing, while cloud choices need a key and network.",
            "fr": "Le TTS parle ; le STT écoute. Le local privilégie le traitement hors ligne ; le cloud demande une clé et un réseau.",
        },
        asset="onboarding/settings-voice.png",
        caption={
            "en": "Sanitized Voice page showing the actual TTS and STT provider controls.",
            "fr": "Page Voix assainie montrant les commandes réelles des fournisseurs TTS et STT.",
        },
        alt_text={
            "en": "OSIA Voice settings with Text-to-Speech provider, voice, Speech-to-Text provider, model and fallback controls. No microphone recording or key is displayed.",
            "fr": "Réglages Voix d’OSIA avec fournisseur et voix TTS, fournisseur, modèle et secours STT. Aucun enregistrement ni clé n’est affiché.",
        },
    ),
    GuideLesson(
        id="advanced.provider-api-hierarchy",
        category=CATEGORY_ADVANCED,
        order=3,
        title={"en": "Provider and API hierarchy", "fr": "Hiérarchie des fournisseurs et API"},
        intro={
            "en": "A provider operates an AI service; an API key is your private access code for that provider.",
            "fr": "Un fournisseur exploite un service d’IA ; une clé API est votre code d’accès privé à ce fournisseur.",
        },
        paragraphs=(
            {
                "en": "ChatGPT Account is a separate managed connection. It does not reuse an OpenAI API key and is never silently preferred. API providers are Anthropic, OpenAI, DeepSeek, Google, xAI, Groq and Mistral; Ollama is local and keyless.",
                "fr": "Le Compte ChatGPT est une connexion gérée séparée. Il ne réutilise pas une clé API OpenAI et n’est jamais privilégié silencieusement. Les fournisseurs API sont Anthropic, OpenAI, DeepSeek, Google, xAI, Groq et Mistral ; Ollama est local et sans clé.",
            },
            {
                "en": "Within one API provider, OSIA uses the ordered saved key chain. The first configured key is primary. On the main chat path, 429/401/403 can advance to the next key before any model switch; shared provider call sites rotate only for technical failures, not ordinary content errors such as 400.",
                "fr": "Pour un fournisseur API, OSIA utilise la liste ordonnée des clés enregistrées. La première est principale. Dans la discussion, les erreurs 429/401/403 peuvent essayer la clé suivante avant tout changement de modèle ; les autres appels ne tournent les clés que pour des erreurs techniques, pas pour une erreur de contenu comme 400.",
            },
            {
                "en": "A provider may also have a primary and fallback API endpoint. A connection or timeout failure can retry the same model and key on the fallback endpoint before key/model fallback. This is not a promise that every outage can be bypassed.",
                "fr": "Un fournisseur peut aussi avoir une adresse API principale et une adresse de secours. Une erreur de connexion ou un délai dépassé peut réessayer le même modèle et la même clé sur l’adresse de secours avant de changer de clé ou de modèle. Cela ne garantit pas de contourner toute panne.",
            },
        ),
        example_title={"en": "Failure order example", "fr": "Exemple d’ordre de secours"},
        example={
            "en": "Selected OpenAI model fails before output: try its endpoint fallback for a network failure → try the next OpenAI key for rate/auth failure → if automatic model routing is enabled, try the next configured model → finally retain the manual model as the chain’s last fallback.",
            "fr": "Le modèle OpenAI choisi échoue avant de répondre : essayer l’adresse de secours en cas de réseau → la clé OpenAI suivante en cas de limite/authentification → le modèle suivant si le routage automatique est actif → conserver le modèle manuel comme dernier secours.",
        },
        remember={
            "en": "Endpoint fallback keeps provider/model/key; key fallback keeps provider/model; model fallback may change both model and provider.",
            "fr": "Le secours d’adresse garde fournisseur/modèle/clé ; le secours de clé garde fournisseur/modèle ; le secours de modèle peut changer modèle et fournisseur.",
        },
        asset="onboarding/settings-api-provider.png",
        caption={
            "en": "The Models page where API providers and protected keys are managed.",
            "fr": "Page Modèles où sont gérés les fournisseurs API et leurs clés protégées.",
        },
        alt_text={
            "en": "OSIA Models settings showing a public provider name and a fully masked demonstration key field.",
            "fr": "Réglages Modèles d’OSIA montrant un nom de fournisseur public et un champ de clé de démonstration entièrement masqué.",
        },
    ),
    GuideLesson(
        id="advanced.ai-model-hierarchy",
        category=CATEGORY_ADVANCED,
        order=4,
        title={"en": "AI and model hierarchy", "fr": "Hiérarchie des IA et modèles"},
        intro={
            "en": "The provider is the service; the model is the particular AI inside that service. Different models trade quality, speed, price and local privacy.",
            "fr": "Le fournisseur est le service ; le modèle est l’IA particulière dans ce service. Les modèles offrent différents compromis de qualité, vitesse, prix et confidentialité locale.",
        },
        paragraphs=(
            {
                "en": "With automatic task routing disabled, OSIA always uses the provider/model you selected manually. This is the default and the easiest behavior to predict.",
                "fr": "Lorsque le routage automatique est désactivé, OSIA utilise toujours le fournisseur et le modèle choisis manuellement. C’est le comportement par défaut et le plus prévisible.",
            },
            {
                "en": "When enabled, a local rule-based classifier labels each request Heavy (coding, debugging, architecture or deep research) or Medium (everything else). OSIA then follows that category’s ordered provider:model chain.",
                "fr": "Lorsqu’il est actif, un classificateur local à base de règles classe chaque demande en Lourde (code, débogage, architecture ou recherche approfondie) ou Moyenne (le reste). OSIA suit alors la chaîne fournisseur:modèle correspondante.",
            },
            {
                "en": "The manually selected model is appended as the final fallback if it is not already in the chain. OSIA advances only on a technical failure before output starts; it does not silently switch for normal content/client errors or replay text that has begun streaming.",
                "fr": "Le modèle choisi manuellement est ajouté en dernier secours s’il n’est pas déjà dans la chaîne. OSIA avance seulement après une erreur technique avant le début de la réponse ; il ne change pas pour une erreur de contenu et ne rejoue pas un texte déjà commencé.",
            },
            {
                "en": "If the selected main model cannot process images, a separately configured vision companion may describe the image first. Local Ollama models can avoid a remote chat request when installed, but capability, speed and memory use depend on the PC and model.",
                "fr": "Si le modèle principal ne comprend pas les images, un modèle visuel séparé peut d’abord les décrire. Les modèles Ollama locaux peuvent éviter un appel de discussion distant s’ils sont installés, mais leurs capacités, vitesse et mémoire dépendent du PC et du modèle.",
            },
        ),
        example_title={"en": "Worked routing example", "fr": "Exemple de routage"},
        example={
            "en": "“Refactor this Python module” is classified Heavy. OSIA tries the first Heavy model, then the next only after an eligible technical failure, and ends with the manually selected model. “Summarize these notes” normally follows the Medium chain.",
            "fr": "« Refactorise ce module Python » est classé Lourd. OSIA essaie le premier modèle Lourd, puis le suivant seulement après une erreur technique admissible, et termine avec le modèle manuel. « Résume ces notes » suit normalement la chaîne Moyenne.",
        },
        remember={
            "en": "Manual selection is the default. Heavy/Medium routing is optional, ordered and failure-driven—not a hidden provider preference.",
            "fr": "La sélection manuelle est la règle. Le routage Lourd/Moyen est facultatif, ordonné et déclenché par les erreurs, sans préférence cachée.",
        },
        asset="onboarding/settings-model-hierarchy.png",
        caption={
            "en": "Sanitized Models page showing the optional Heavy and Medium task chains.",
            "fr": "Page Modèles assainie montrant les chaînes de tâches Lourde et Moyenne facultatives.",
        },
        alt_text={
            "en": "OSIA AI task hierarchy controls with automatic routing and separate Heavy and Medium ordered model rows. Only public provider and model labels appear.",
            "fr": "Commandes de hiérarchie IA d’OSIA avec routage automatique et listes ordonnées Lourde et Moyenne. Seuls des noms publics apparaissent.",
        },
    ),
    GuideLesson(
        id="advanced.memory-conversations",
        category=CATEGORY_ADVANCED,
        order=5,
        title={"en": "Memory and conversations", "fr": "Mémoire et conversations"},
        intro={
            "en": "Conversation history and persistent memory are related, but they are not the same thing.",
            "fr": "L’historique des conversations et la mémoire persistante sont liés, mais différents.",
        },
        paragraphs=(
            {
                "en": "The current conversation is the active sequence of messages. Saved conversations are local history files that you can reopen from Conversations so OSIA receives the relevant prior messages again.",
                "fr": "La conversation actuelle est la suite active de messages. Les conversations enregistrées sont des historiques locaux que vous pouvez rouvrir depuis Conversations afin qu’OSIA retrouve les messages précédents utiles.",
            },
            {
                "en": "Persistent memory is a separate local store for verified facts, preferences, routes, contacts, shortcuts or workflows. Relevant entries may be added to a future request even when you open another conversation.",
                "fr": "La mémoire persistante est un stockage local séparé pour des faits, préférences, routes, contacts, raccourcis ou méthodes vérifiés. Les éléments pertinents peuvent être ajoutés à une demande future, même dans une autre conversation.",
            },
            {
                "en": "Settings → Memory lets you review, edit, delete individual entries or clear all memory. Conversation controls manage saved chats separately. OSIA’s memory store rejects common secret/token patterns, but you should still never ask it to remember passwords, API keys or pairing tokens.",
                "fr": "Réglages → Mémoire permet de consulter, modifier, supprimer une entrée ou tout effacer. Les commandes Conversations gèrent les discussions séparément. La mémoire refuse des formes courantes de secrets, mais ne demandez jamais à OSIA de mémoriser mots de passe, clés API ou jetons d’association.",
            },
            {
                "en": "Both stores are on the PC profile, so anyone with access to that profile may be able to read them. Delete sensitive history or memory you no longer need and protect the computer account.",
                "fr": "Les deux stockages se trouvent dans le profil PC ; une personne ayant accès à ce profil pourrait les lire. Supprimez les historiques ou souvenirs sensibles devenus inutiles et protégez le compte de l’ordinateur.",
            },
        ),
        example_title={"en": "The difference", "fr": "La différence"},
        example={
            "en": "Conversation: “Yesterday we compared two travel plans” stays with that saved chat.\nMemory: “I prefer morning appointments” may be relevant across future chats until you edit or delete it.",
            "fr": "Conversation : « Hier, nous avons comparé deux voyages » reste dans cette discussion enregistrée.\nMémoire : « Je préfère les rendez-vous le matin » peut servir dans de futures discussions jusqu’à modification ou suppression.",
        },
        remember={
            "en": "Conversations preserve chats; memory preserves selected reusable facts. Review and delete them through their separate controls.",
            "fr": "Les conversations conservent les discussions ; la mémoire conserve certains faits réutilisables. Gérez-les avec leurs commandes distinctes.",
        },
    ),
    GuideLesson(
        id="advanced.personality-skills-automation",
        category=CATEGORY_ADVANCED,
        order=6,
        title={"en": "Personality, skills and automation", "fr": "Personnalité, compétences et automatisation"},
        intro={
            "en": "Personality changes how OSIA communicates; skills add reusable instructions; tools perform supported work; automations schedule requests.",
            "fr": "La personnalité change la façon de communiquer d’OSIA ; les compétences ajoutent des instructions réutilisables ; les outils effectuent le travail pris en charge ; les automatisations planifient des demandes.",
        },
        paragraphs=(
            {
                "en": "The OSIA page edits a local personality document that is added to the assistant instructions. Example: ask for concise explanations and a calm tone. Personality changes style, not permissions or factual capability.",
                "fr": "La page OSIA modifie un document local de personnalité ajouté aux instructions de l’assistant. Exemple : demander des explications concises et un ton calme. La personnalité change le style, pas les autorisations ni les capacités réelles.",
            },
            {
                "en": "Skills are local Markdown instruction files that can be created, edited, enabled or disabled. Example: a meeting-notes skill can define your preferred headings. A skill guides the model; it does not create a missing integration or bypass safety checks.",
                "fr": "Les compétences sont des fichiers d’instructions Markdown locaux pouvant être créés, modifiés, activés ou désactivés. Exemple : une compétence de compte rendu peut définir vos titres préférés. Elle guide le modèle sans créer une intégration absente ni contourner la sécurité.",
            },
            {
                "en": "Tools are the actual supported operations OSIA may call, including PC tools and configured MCP connectors. External write actions may require confirmation; unattended connector writes are a separate advanced safety choice and are off by default.",
                "fr": "Les outils sont les opérations réellement prises en charge, y compris les outils PC et les connecteurs MCP configurés. Les écritures externes peuvent demander confirmation ; les écritures sans surveillance sont un choix de sécurité avancé, désactivé par défaut.",
            },
            {
                "en": "Settings → Advanced can schedule repeated or one-time tasks and optionally bind a conversation/model. The PC process must be running when a task is due. Heartbeat monitoring can report selected PC health thresholds; it is monitoring, not automatic repair.",
                "fr": "Réglages → Avancé peut planifier des tâches répétées ou ponctuelles et leur associer une conversation/un modèle. Le processus PC doit fonctionner à l’heure prévue. La surveillance peut signaler certains seuils du PC ; elle ne répare pas automatiquement.",
            },
        ),
        example_title={"en": "Safe examples", "fr": "Exemples sûrs"},
        example={
            "en": "Personality: “Explain unfamiliar terms before using them.”\nSkill: a reusable weekly-review format.\nTool: read a named local file and summarize it.\nAutomation: every weekday at 17:00, prepare a review in a chosen conversation—only while OSIA is running.",
            "fr": "Personnalité : « Explique les termes inconnus avant de les utiliser. »\nCompétence : un format réutilisable de bilan hebdomadaire.\nOutil : lire un fichier local nommé et le résumer.\nAutomatisation : chaque semaine à 17 h, préparer un bilan dans une conversation choisie—seulement si OSIA fonctionne.",
        },
        remember={
            "en": "Instructions shape behavior; tools provide capability; schedules trigger work. None should be treated as permission to ignore confirmations or review.",
            "fr": "Les instructions façonnent le comportement ; les outils donnent la capacité ; les horaires déclenchent le travail. Aucun ne remplace les confirmations ni la vérification.",
        },
    ),
    GuideLesson(
        id="advanced.updates-safety",
        category=CATEGORY_ADVANCED,
        order=7,
        title={"en": "Updates and safety", "fr": "Mises à jour et sécurité"},
        intro={
            "en": "OSIA checks for available versions and presents changes in the dedicated Settings → Updates page.",
            "fr": "OSIA recherche les versions disponibles et présente les changements dans Réglages → Mises à jour.",
        },
        paragraphs=(
            {
                "en": "Update detection runs automatically on its schedule and can also be requested from the page. When an update is available, OSIA shows the installed/available versions and embedded release notes before installation.",
                "fr": "La détection s’exécute automatiquement selon son calendrier et peut aussi être demandée depuis la page. Lorsqu’une version existe, OSIA affiche les versions installée/disponible et les notes intégrées avant installation.",
            },
            {
                "en": "The Windows updater downloads the package, verifies the published metadata and package integrity, starts the installer, preserves the user profile, and uses a rollback path if replacing the application fails. Do not interrupt the installer or delete its working files.",
                "fr": "Sous Windows, le programme télécharge le paquet, vérifie les métadonnées publiées et son intégrité, lance l’installation, préserve le profil utilisateur et prévoit un retour arrière si le remplacement échoue. N’interrompez pas l’installation et ne supprimez pas ses fichiers temporaires.",
            },
            {
                "en": "Unsigned internal or preview builds can trigger a Windows warning because the operating system cannot verify a trusted publisher signature. Only continue when you intentionally obtained that exact build from a trusted OSIA source.",
                "fr": "Les versions internes ou d’aperçu non signées peuvent déclencher un avertissement Windows, car l’éditeur n’est pas vérifiable. Continuez uniquement si vous avez obtenu volontairement cette version précise depuis une source OSIA fiable.",
            },
            {
                "en": "Never share API keys, account codes, pairing tokens, OAuth data, diagnostic logs containing secrets, certificates or screenshots with private fields revealed. Regenerate a secret if it may have escaped.",
                "fr": "Ne partagez jamais clés API, codes de compte, jetons d’association, données OAuth, journaux contenant des secrets, certificats ou captures avec champs privés visibles. Régénérez tout secret potentiellement divulgué.",
            },
        ),
        example_title={"en": "Before updating or asking for help", "fr": "Avant une mise à jour ou une demande d’aide"},
        example={
            "en": "Read the release notes → confirm the source/version → close important work → install → verify OSIA starts and your profile remains. For support, reproduce with sanitized details and mask every secret first.",
            "fr": "Lire les notes → confirmer source et version → fermer les travaux importants → installer → vérifier le démarrage et le profil. Pour l’assistance, reproduire avec des détails assainis et masquer d’abord chaque secret.",
        },
        remember={
            "en": "The updater protects package integrity and profile data, but you still control when to install and what private diagnostics to share.",
            "fr": "Le programme protège l’intégrité du paquet et le profil, mais vous contrôlez le moment de l’installation et les diagnostics privés partagés.",
        },
    ),
    GuideLesson(
        id="advanced.usage-examples",
        category=CATEGORY_ADVANCED,
        order=8,
        title={"en": "Advanced usage examples", "fr": "Exemples d’utilisation avancée"},
        intro={
            "en": "Advanced use is mainly about giving OSIA a clear sequence, choosing the right connection/model, and checking results at the right moments.",
            "fr": "L’utilisation avancée consiste surtout à donner une séquence claire, choisir la bonne connexion ou le bon modèle et vérifier les résultats au bon moment.",
        },
        paragraphs=(
            {
                "en": "Multi-step work: “Read these three notes, list disagreements, propose one plan, then stop before creating any external event.” This gives OSIA an order and a safety boundary.",
                "fr": "Travail en plusieurs étapes : « Lis ces trois notes, liste les désaccords, propose un plan, puis arrête-toi avant de créer un événement externe. » La séquence et la limite sont explicites.",
            },
            {
                "en": "PC and mobile: start a document summary on the PC, reopen the same conversation on the phone, and ask for a shorter checklist. Voice: dictate the goal, then review names and dates in the written transcript before requesting an action.",
                "fr": "PC et mobile : commencez un résumé sur le PC, rouvrez la même conversation sur le téléphone et demandez une liste plus courte. Voix : dictez le but, puis vérifiez noms et dates dans la transcription avant une action.",
            },
            {
                "en": "Models: use manual selection for predictable tests; enable Heavy/Medium chains only when you want automatic routing and have configured each entry. Compare quality, latency and provider usage with the same harmless prompt.",
                "fr": "Modèles : utilisez la sélection manuelle pour des tests prévisibles ; activez les chaînes Lourde/Moyenne seulement après avoir configuré chaque entrée. Comparez qualité, délai et utilisation avec la même demande sans risque.",
            },
            {
                "en": "Troubleshooting: provider failure → verify selected provider/model, key and account limits; mobile failure → verify PC/server is running, token matches and network is available; memory surprise → review the Memory page and edit/delete the entry rather than correcting it repeatedly in chat.",
                "fr": "Dépannage : échec fournisseur → vérifier fournisseur/modèle, clé et limites du compte ; échec mobile → vérifier PC/serveur, jeton et réseau ; mémoire inattendue → consulter la page Mémoire et modifier/supprimer l’entrée plutôt que la corriger à chaque discussion.",
            },
        ),
        example_title={"en": "One complete workflow", "fr": "Un flux complet"},
        example={
            "en": "1. On PC, ask OSIA to analyze a local error log without changing files.\n2. Review its diagnosis and choose one proposed fix.\n3. Continue from mobile and ask for a concise execution checklist.\n4. Return to PC for any tool action and verify the final diff/output.",
            "fr": "1. Sur le PC, demander l’analyse d’un journal d’erreurs sans modifier de fichier.\n2. Vérifier le diagnostic et choisir une correction proposée.\n3. Continuer sur mobile et demander une liste d’exécution concise.\n4. Revenir au PC pour toute action d’outil et vérifier le résultat final.",
        },
        remember={
            "en": "Break work into stages, preserve context in the conversation, and place a review point before consequential actions.",
            "fr": "Découpez le travail, conservez le contexte dans la conversation et prévoyez une vérification avant les actions importantes.",
        },
    ),
)


CurriculumSpec = tuple[
    str, str, str, str, str, str, str, str, str, str, str, str, tuple[str, ...]
]


def _curriculum_lesson(
    category: str, order: int, spec: CurriculumSpec,
) -> GuideLesson:
    (
        lesson_id,
        subject_id,
        title_en,
        title_fr,
        intro_en,
        intro_fr,
        detail_en,
        detail_fr,
        example_en,
        example_fr,
        remember_en,
        remember_fr,
        glossary_ids,
    ) = spec
    detail_parts_en = detail_en.split("\n\n")
    detail_parts_fr = detail_fr.split("\n\n")
    if len(detail_parts_en) != len(detail_parts_fr):
        raise ValueError(f"guide_localization_section_mismatch:{lesson_id}")
    return GuideLesson(
        id=lesson_id,
        category=category,
        order=order,
        title={"en": title_en, "fr": title_fr},
        intro={"en": intro_en, "fr": intro_fr},
        paragraphs=tuple(
            {"en": value_en, "fr": value_fr}
            for value_en, value_fr in zip(detail_parts_en, detail_parts_fr)
        ),
        example_title={"en": "In practice", "fr": "En pratique"},
        example={"en": example_en, "fr": example_fr},
        remember={"en": remember_en, "fr": remember_fr},
        subject_id=subject_id,
        glossary_ids=glossary_ids,
    )


_LEGACY_MASTERY_METADATA = {
    "advanced.provider-api-hierarchy": (
        4, "provider-hierarchy", "Provider and API hierarchy",
        "Hiérarchie des fournisseurs et API",
    ),
    "advanced.ai-model-hierarchy": (
        5, "model-hierarchy", "Models and AI hierarchy",
        "Modèles et hiérarchie IA",
    ),
    "advanced.personality-skills-automation": (
        10, "scheduled-tasks", "Scheduled tasks", "Tâches planifiées",
    ),
    "advanced.memory-conversations": (
        12, "memory-context", "Memory and context", "Mémoire et contexte",
    ),
    "advanced.voice-tts-stt": (
        14, "voice", "Voice", "Voix",
    ),
    "advanced.pc-mobile-connection": (
        15, "pairing-connection", "Pairing and connection",
        "Association et connexion",
    ),
    "advanced.updates-safety": (
        18, "updates", "Updates", "Mises à jour",
    ),
    "advanced.usage-examples": (
        20, "practical-workflows", "Practical workflows",
        "Flux de travail pratiques",
    ),
}


_MASTER_NEW_SPECS: tuple[CurriculumSpec, ...] = (
    (
        "mastery.mental-model", "mental-model", "OSIA's mental model",
        "Le modèle mental d’OSIA",
        "OSIA executes work and keeps its main state on the PC; the phone is a secure second screen.",
        "OSIA exécute le travail et conserve son état principal sur le PC ; le téléphone est un second écran sécurisé.",
        "Conversations, memory, providers, scheduled tasks and tool execution belong to the PC. Reinstalling the mobile app does not erase PC conversations, but most mobile actions require that PC to be awake, running and reachable.",
        "Les conversations, la mémoire, les fournisseurs, les tâches planifiées et les outils appartiennent au PC. Réinstaller l’app mobile n’efface pas les conversations du PC, mais la plupart des actions mobiles exigent un PC actif, démarré et joignable.",
        "Start a plan on the PC, continue it from the phone, then return to the PC for a sensitive computer action.",
        "Commencez un plan sur le PC, poursuivez-le depuis le téléphone, puis revenez au PC pour une action sensible.",
        "Back up and manage the PC profile as the source of truth.",
        "Sauvegardez et gérez le profil PC comme source de vérité.",
        (),
    ),
    (
        "mastery.conversations-turns", "conversations-turns",
        "Conversations and turns", "Conversations et tours",
        "A conversation stores related messages; a turn is one request and its streamed result.",
        "Une conversation regroupe des messages liés ; un tour correspond à une demande et à son résultat diffusé progressivement.",
        "Create or select a conversation before sending. PC and mobile synchronize history through the PC. While a turn is active, a second prompt can be blocked to prevent mixed tool results; Stop cancels the active work, and Retry should be used only after its terminal state is visible.",
        "Créez ou choisissez une conversation avant l’envoi. PC et mobile synchronisent l’historique via le PC. Pendant un tour actif, une seconde demande peut être bloquée pour éviter de mélanger les outils ; Arrêter annule le travail et Réessayer ne doit être utilisé qu’après l’état final.",
        "If a long reply is streaming, wait or stop it before asking a different question in that conversation.",
        "Si une longue réponse arrive progressivement, attendez ou arrêtez-la avant une autre demande dans cette conversation.",
        "A blocked second prompt protects turn ordering; it is not a lost message.",
        "Le blocage d’une seconde demande protège l’ordre des tours ; ce n’est pas un message perdu.",
        (),
    ),
    (
        "mastery.providers-accounts", "providers-accounts",
        "Providers and accounts", "Fournisseurs et comptes",
        "A provider operates the AI service; an account or API key authorizes OSIA to use it.",
        "Un fournisseur exploite le service d’IA ; un compte ou une clé API autorise OSIA à l’utiliser.",
        "ChatGPT Account uses its isolated managed sign-in. API providers exposed in Models use their own keys, quotas, availability and privacy terms; local Ollama remains a separate local option when installed. Status and errors belong to the selected route, not to OSIA as a promise of service.",
        "Compte ChatGPT utilise une connexion gérée et isolée. Les fournisseurs API affichés dans Modèles ont leurs propres clés, quotas, disponibilités et règles de confidentialité ; Ollama reste une option locale distincte s’il est installé. L’état et les erreurs dépendent de la route choisie.",
        "Connect one route, select one compatible model, then send a harmless test prompt before adding fallbacks.",
        "Connectez une route, choisissez un modèle compatible, puis envoyez une demande sans risque avant d’ajouter des replis.",
        "Provider availability, price and data handling can change outside OSIA.",
        "Disponibilité, prix et traitement des données peuvent changer hors d’OSIA.",
        (),
    ),
    (
        "mastery.reasoning", "reasoning", "Thinking and reasoning",
        "Réflexion et raisonnement",
        "Reasoning levels trade speed for deeper model work on models that support them.",
        "Les niveaux de raisonnement échangent de la vitesse contre un travail plus approfondi sur les modèles compatibles.",
        "OSIA exposes Auto, Off, Low, Medium and High where supported. Higher settings can take longer and consume more quota; unsupported models ignore the control. Use lower levels for simple formatting and higher levels for difficult planning or diagnosis.",
        "OSIA propose Auto, Désactivé, Faible, Moyen et Élevé lorsque le modèle le permet. Un niveau supérieur peut être plus lent et consommer davantage de quota ; les modèles incompatibles ignorent ce réglage. Préférez un niveau faible pour le formatage et élevé pour un diagnostic complexe.",
        "Compare the same safe prompt at Low and High before choosing a default.",
        "Comparez la même demande sans risque en Faible puis Élevé avant de choisir.",
        "A higher level does not guarantee a correct answer; review important results.",
        "Un niveau supérieur ne garantit pas une réponse exacte ; vérifiez les résultats importants.",
        (),
    ),
    (
        "mastery.keys-quotas-security", "keys-quotas-security",
        "Keys, quotas and security", "Clés, quotas et sécurité",
        "Provider keys are PC-owned secrets and must never be pasted into a conversation.",
        "Les clés de fournisseur sont des secrets gérés par le PC et ne doivent jamais être collées dans une conversation.",
        "Add, replace or remove keys in Models. OSIA can rotate through configured backup keys for technical failures such as authentication, rate limits, timeouts or server errors, but not for a content error such as an invalid request. The paired mobile editor masks keys and handles edits only over the authenticated encrypted connection.",
        "Ajoutez, remplacez ou supprimez les clés dans Modèles. OSIA peut essayer des clés de secours lors d’erreurs techniques d’authentification, de quota, de délai ou de serveur, mais pas pour une requête invalide. L’éditeur mobile associé masque les clés et les traite uniquement sur la connexion chiffrée authentifiée.",
        "For a 401 error, verify provider and key; for a 429, inspect quota before retrying or adding an authorized backup key.",
        "Pour une erreur 401, vérifiez fournisseur et clé ; pour une 429, contrôlez le quota avant de réessayer ou d’ajouter une clé autorisée.",
        "Treat phone access, screenshots and diagnostics as part of the secret boundary.",
        "Considérez l’accès au téléphone, les captures et les diagnostics comme faisant partie de la frontière des secrets.",
        (),
    ),
    (
        "mastery.files-images", "files-images", "Files and images",
        "Fichiers et images",
        "PC drafts and mobile batches let one turn combine text with one or more files.",
        "Les brouillons PC et les lots mobiles permettent de combiner texte et fichiers dans un seul tour.",
        "On PC, files remain staged until Send and can be removed individually; attachment-only turns are allowed. Mobile accepts up to ten files and 20 MiB for each file and the whole batch, validates every chunk and commits atomically. Images require both a provider and model with vision support; unsupported combinations fail clearly before the provider call.",
        "Sur PC, les fichiers restent en brouillon jusqu’à Envoyer et peuvent être retirés séparément ; un tour sans texte est permis. Le mobile accepte jusqu’à dix fichiers et 20 Mio par fichier et par lot, valide chaque fragment puis valide le lot atomiquement. Les images exigent un fournisseur et un modèle compatibles avec la vision.",
        "Attach a screenshot with the question “Explain this visible error,” then verify that a compatible vision model is selected.",
        "Joignez une capture avec « Explique cette erreur visible », puis vérifiez qu’un modèle de vision compatible est sélectionné.",
        "PC limits follow measured resources and provider policy; mobile has the explicit ten-file, 20 MiB aggregate policy.",
        "Les limites PC suivent les ressources mesurées et le fournisseur ; le mobile applique explicitement dix fichiers et 20 Mio au total.",
        (),
    ),
    (
        "mastery.autonomous", "autonomous", "Autonomous mode",
        "Mode autonome",
        "Autonomous mode is for a bounded multi-step mission, not a routine follow-up question.",
        "Le mode autonome sert à une mission bornée en plusieurs étapes, pas à une question de suivi ordinaire.",
        "Describe the objective, choose its provider/model and monitor phases, tasks and confirmations. One machine-wide runner is allowed. Interrupt pauses a resumable checkpoint; Abandon ends it and removes that checkpoint. Plans and retries are bounded, and external writes still follow confirmation policy.",
        "Décrivez l’objectif, choisissez son fournisseur/modèle et surveillez phases, tâches et confirmations. Une seule mission globale peut tourner. Interrompre met en pause un point de reprise ; Abandonner termine la mission et le retire. Plans et tentatives sont bornés, et les écritures externes gardent leurs confirmations.",
        "Use it to inspect a project, propose a plan and apply approved fixes; use normal chat to rephrase one paragraph.",
        "Utilisez-le pour inspecter un projet, proposer un plan et appliquer des corrections approuvées ; utilisez le chat pour reformuler un paragraphe.",
        "Keep consequential work supervised and preserve backups.",
        "Surveillez les actions importantes et conservez des sauvegardes.",
        (),
    ),
    (
        "mastery.connectors", "connectors", "MCP and connectors",
        "MCP et connecteurs",
        "A connector adds tools from another service; a catalog entry is not the same as a connected service.",
        "Un connecteur ajoute les outils d’un autre service ; une entrée de catalogue n’est pas un service connecté.",
        "Connectors may use OAuth, a bearer token, local stdio or remote HTTP according to their catalog entry. Connected tools are namespaced and may request confirmation before writes; unattended connector writes are denied by default. Mobile mirrors status and confirmations, while unavailable catalog entries remain clearly unavailable.",
        "Selon le catalogue, un connecteur utilise OAuth, un jeton, stdio local ou HTTP distant. Les outils connectés sont nommés séparément et peuvent demander confirmation avant une écriture ; les écritures sans surveillance sont refusées par défaut. Le mobile reflète états et confirmations.",
        "Connect one supported service, inspect the tools it adds, then begin with a read-only request.",
        "Connectez un service pris en charge, examinez les outils ajoutés, puis commencez par une lecture seule.",
        "Never assume a visible catalog card is currently connectable.",
        "Ne supposez jamais qu’une carte visible du catalogue est actuellement connectable.",
        (),
    ),
    (
        "mastery.computer-use", "tools-computer-control",
        "Tools and computer control", "Outils et contrôle de l’ordinateur",
        "OSIA prefers deterministic integrations before visual guesses when it operates the computer.",
        "OSIA préfère les intégrations déterministes aux suppositions visuelles lorsqu’il agit sur l’ordinateur.",
        "It can use deep links, command-line tools, browser structure, Windows UI Automation and keyboard input, then fall back to screenshots and vision. Coordinates can be uncertain when layouts move. Confirm sensitive actions, state the target precisely and verify the resulting window, file or page.",
        "Il peut utiliser liens profonds, ligne de commande, structure du navigateur, UI Automation Windows et clavier, puis se replier sur captures et vision. Les coordonnées deviennent incertaines si la mise en page bouge. Confirmez les actions sensibles et vérifiez fenêtre, fichier ou page obtenus.",
        "Ask OSIA to open a named settings page and report what it sees before requesting a change.",
        "Demandez à OSIA d’ouvrir une page de réglages nommée et de décrire son état avant une modification.",
        "Do not delegate irreversible actions without a review point.",
        "Ne déléguez pas d’action irréversible sans point de contrôle.",
        (),
    ),
    (
        "mastery.notifications-background", "notifications-background",
        "Notifications and background behavior", "Notifications et arrière-plan",
        "Notifications report results; they do not keep an unavailable PC executing work.",
        "Les notifications signalent des résultats ; elles ne font pas travailler un PC indisponible.",
        "PC notifications and Android local notifications are best effort and depend on system permissions. The Android foreground service can improve continuity but remains subject to Android power rules. Scheduled and autonomous work still needs OSIA running; after sleep or a network change, reopen the app and allow one clean reconnect.",
        "Les notifications PC et Android sont fournies au mieux et dépendent des autorisations système. Le service Android au premier plan améliore la continuité mais reste soumis aux règles d’énergie. Les tâches ont toujours besoin d’OSIA actif ; après veille ou changement réseau, rouvrez l’app et laissez une reconnexion propre.",
        "If a task completed without an alert, check its in-app result before changing the schedule.",
        "Si une tâche s’est terminée sans alerte, consultez son résultat dans l’app avant de modifier l’horaire.",
        "Notification permission and execution availability are separate concerns.",
        "L’autorisation de notification et la disponibilité d’exécution sont deux sujets distincts.",
        (),
    ),
    (
        "mastery.settings-tour", "settings-tour", "Settings tour",
        "Visite des réglages",
        "Settings groups the controls that shape OSIA without changing conversation history.",
        "Les Réglages regroupent les contrôles qui configurent OSIA sans modifier l’historique des conversations.",
        "General covers language, appearance, capsule, startup and this guide. Models covers accounts, API keys, models, reasoning and hierarchies. Voice and Wake word configure audio. Memory reviews durable facts. MCP / Connectors manages integrations. Connection / Mobile manages pairing and server startup. Advanced contains schedules and PC health; Updates and Legal show release and policy information.",
        "Général couvre langue, apparence, capsule, démarrage et ce guide. Modèles couvre comptes, clés API, modèles, raisonnement et hiérarchies. Voix et Mot d’activation règlent l’audio. Mémoire examine les faits durables. MCP / Connecteurs gère les intégrations. Connexion / Mobile gère association et serveur. Avancé contient horaires et santé du PC ; Mises à jour et Légal affichent versions et règles.",
        "After changing a model or voice, run a short harmless test before starting important work.",
        "Après un changement de modèle ou de voix, lancez un essai court et sans risque.",
        "Security-related toggles change permission boundaries; read their descriptions first.",
        "Les réglages de sécurité modifient les limites d’autorisation ; lisez leur description.",
        (),
    ),
    (
        "mastery.troubleshooting", "troubleshooting", "Troubleshooting",
        "Dépannage",
        "Diagnose the layer that failed before changing several settings at once.",
        "Identifiez la couche en échec avant de modifier plusieurs réglages à la fois.",
        "For keys or quota, inspect provider status and the selected model. For images, verify vision support and provider limits. For mobile, check the PC, pairing and one clean reconnect. For voice or notifications, check permissions and devices. For schedules, confirm the PC was running at the due time. For MCP or updates, verify connection status and availability.",
        "Pour clé ou quota, vérifiez le fournisseur et le modèle. Pour une image, contrôlez vision et limites. Pour le mobile, vérifiez PC, association et reconnexion. Pour voix ou notifications, contrôlez autorisations et appareils. Pour une tâche, confirmez que le PC tournait à l’heure prévue. Pour MCP ou mise à jour, vérifiez connexion et disponibilité.",
        "Reproduce with one short request and record the sanitized error category, not secrets or full private logs.",
        "Reproduisez avec une demande courte et notez la catégorie d’erreur assainie, jamais les secrets ni des journaux privés complets.",
        "One precise observation is more useful than repeated blind retries.",
        "Une observation précise vaut mieux que des tentatives répétées à l’aveugle.",
        (),
    ),
)


_GLOSSARY_IDS = (
    "provider", "model", "api", "token", "context", "thinking", "mcp",
    "dcr", "oauth", "cron", "websocket", "e2e", "lan", "p2p", "webrtc",
    "sdp", "ice", "stun", "turn", "signaling", "relay", "durable-object",
    "hibernation", "chunking", "hash", "mime", "abi", "apk", "aab", "r8",
    "pyinstaller", "inno-setup", "r2", "rollout", "version-name",
    "version-code",
)


_ADVANCED_SPECS: tuple[CurriculumSpec, ...] = (
    (
        "advanced.product-architecture", "product-architecture",
        "Product architecture", "Architecture du produit",
        "OSIA is a PC-first assistant with a mobile control surface and pluggable AI providers.",
        "OSIA est un assistant centré sur le PC, avec une surface mobile et des fournisseurs d’IA interchangeables.",
        "Execution, durable state and tools stay on the PC. The mobile is a second screen, networking prefers direct authenticated paths, provider adapters isolate external APIs, and the agent invokes only registered tools under confirmation policy.",
        "L’exécution, l’état durable et les outils restent sur le PC. Le mobile est un second écran, le réseau préfère les chemins directs authentifiés, les adaptateurs isolent les API externes et l’agent n’appelle que les outils enregistrés selon la politique de confirmation.",
        "Trace any feature through UI, PC state owner, transport, agent/provider and terminal result.",
        "Suivez toute fonction à travers interface, propriétaire PC, transport, agent/fournisseur et résultat final.",
        "Local-first describes ownership, not a promise that configured cloud providers receive no data.",
        "Local-first décrit la propriété, pas la promesse qu’aucun fournisseur cloud configuré ne reçoit de données.",
        ("provider", "model", "api"),
    ),
    (
        "advanced.repository-topology", "repository-topology",
        "Repository topology", "Topologie des dépôts",
        "Four independently versioned repositories divide product responsibilities.",
        "Quatre dépôts versionnés indépendamment répartissent les responsabilités du produit.",
        "osia contains the cross-platform PC application; osia-mobile contains Flutter clients; osia-worker contains rendezvous and relay Workers; osia-site contains the public static site and release metadata presentation. Coordinated changes must audit each repository without assuming a shared release number.",
        "osia contient l’application PC multiplateforme ; osia-mobile les clients Flutter ; osia-worker les Workers de rendez-vous et relais ; osia-site le site statique public et la présentation des métadonnées. Une modification coordonnée audite chaque dépôt sans supposer un numéro commun.",
        "A transport change normally needs PC, mobile and Worker compatibility review, while a guide-only site explanation may remain read-only.",
        "Un changement de transport exige normalement une revue PC, mobile et Worker, tandis qu’une explication de guide peut laisser le site en lecture seule.",
        "Record each repository HEAD and divergence separately.",
        "Consignez séparément le HEAD et la divergence de chaque dépôt.",
        (),
    ),
    (
        "advanced.pc-architecture", "pc-architecture", "PC architecture",
        "Architecture PC",
        "The PC application combines a Qt interface with local services and an agent runtime.",
        "L’application PC combine une interface Qt, des services locaux et un agent.",
        "Key modules include osia/gui for Qt, main.py for startup, agent.py for turns, conversation storage, models.py and provider adapters, attachments.py and desktop_attachments.py, vision routing, mcp_client, cron.py, memory.py, LAN/relay/WebRTC modules, updater modules and packaging/windows. Protected parsing and transport contracts require targeted changes only.",
        "Les modules clés comprennent osia/gui pour Qt, main.py pour le démarrage, agent.py pour les tours, le stockage des conversations, models.py et les adaptateurs, attachments.py et desktop_attachments.py, la vision, mcp_client, cron.py, memory.py, LAN/relais/WebRTC, la mise à jour et packaging/windows. Les contrats protégés exigent des changements ciblés.",
        "When a GUI action changes state, find its signal, service call, persistence file and every caller before editing.",
        "Quand une action GUI change l’état, trouvez son signal, l’appel de service, le fichier persistant et tous les appelants avant modification.",
        "Module names are navigation aids, not permission to refactor adjacent code.",
        "Les noms de modules aident à naviguer ; ils n’autorisent pas un refactoring voisin.",
        ("api", "provider", "model"),
    ),
    (
        "advanced.mobile-architecture", "mobile-architecture",
        "Mobile architecture", "Architecture mobile",
        "The Flutter application renders screens while services mirror PC-owned state and select one transport.",
        "L’application Flutter affiche les écrans tandis que les services reflètent l’état du PC et choisissent un transport.",
        "lib/screens holds user surfaces; WebSocketService is the message facade; transport, LAN, WebRTC and relay services coordinate connectivity; storage keeps pairing and interface preferences; lifecycle, foreground service and notifications handle platform constraints. Android and iOS projects provide native shells, but the mobile is not the conversation source of truth.",
        "lib/screens contient les surfaces ; WebSocketService est la façade de messages ; les services transport, LAN, WebRTC et relais coordonnent la connexion ; le stockage garde association et préférences ; cycle de vie, service au premier plan et notifications gèrent les contraintes. Android et iOS sont des coques, pas la source des conversations.",
        "Follow a mobile button from screen callback to service frame and PC acknowledgement.",
        "Suivez un bouton mobile depuis le rappel d’écran jusqu’à la trame de service et l’accusé PC.",
        "A locally cached selection is not automatically authoritative PC configuration.",
        "Une sélection en cache local n’est pas automatiquement la configuration PC autoritaire.",
        ("websocket",),
    ),
    (
        "advanced.worker-architecture", "worker-architecture",
        "Worker architecture", "Architecture Worker",
        "Cloudflare Workers provide rendezvous and an E2E-blind WebSocket relay.",
        "Les Cloudflare Workers fournissent le rendez-vous et un relais WebSocket aveugle au contenu E2E.",
        "The rendezvous Worker maps a derived key to the PC route and relays OAuth callbacks. The relay uses one Durable Object per pair, role-authoritative URLs, WebSocket hibernation, sequence and size guards, priority-aware backpressure and generation replacement. It sees routing metadata and ciphertext, not decrypted conversations, tools, files or audio.",
        "Le Worker de rendez-vous associe une clé dérivée à la route PC et relaie les retours OAuth. Le relais utilise un Durable Object par paire, des URL autoritaires par rôle, l’hibernation WebSocket, des gardes de séquence/taille, une pression selon priorité et le remplacement de génération. Il voit métadonnées et chiffré, pas le contenu.",
        "Inspect relay policy and endpoint crypto together before changing an envelope field.",
        "Examinez ensemble politique du relais et crypto des extrémités avant de changer une enveloppe.",
        "Worker routing authority must never become decryption authority.",
        "L’autorité de routage du Worker ne doit jamais devenir une autorité de déchiffrement.",
        ("websocket", "e2e", "durable-object", "hibernation", "relay"),
    ),
    (
        "advanced.message-flow", "message-flow", "End-to-end message flow",
        "Flux de message de bout en bout",
        "A user request is correlated from its conversation through one terminal result.",
        "Une demande utilisateur est corrélée depuis sa conversation jusqu’à un résultat final unique.",
        "The UI assigns conversation and request identity, attachments prepare before commit, the PC locks the turn, selects provider/model, assembles context and runs the agent. Tool progress and streamed text carry correlation back to PC and mobile; completion, failure or cancellation seals the turn and releases the lock.",
        "L’interface attribue conversation et requête, les pièces jointes se préparent avant validation, le PC verrouille le tour, choisit fournisseur/modèle, assemble le contexte et lance l’agent. Progression et texte diffusé reviennent corrélés ; fin, échec ou annulation scellent le tour et libèrent le verrou.",
        "Draw the flow as input → attachment commit → active turn → provider/tools → stream → terminal event.",
        "Dessinez le flux : entrée → validation des pièces jointes → tour actif → fournisseur/outils → flux → événement final.",
        "Never replay a prior answer as success after an attachment or provider failure.",
        "Ne rejouez jamais une ancienne réponse comme succès après un échec de fichier ou de fournisseur.",
        ("context", "provider", "model"),
    ),
    (
        "advanced.pairing-protocol", "pairing-protocol", "Pairing protocol",
        "Protocole d’association",
        "Temporary pairing material establishes trust without publishing the raw secret.",
        "Le matériel temporaire d’association établit la confiance sans publier le secret brut.",
        "PC and mobile derive a pair identifier and proofs, assert authoritative roles and confirm fresh cryptographic context. Expired or mismatched material fails closed; regenerating the PC token replaces trust and requires pairing again. Reconnect creates a new session or generation rather than accepting stale identity.",
        "PC et mobile dérivent un identifiant de paire et des preuves, affirment leurs rôles et confirment un contexte cryptographique neuf. Un matériel expiré ou incohérent échoue fermé ; régénérer le jeton remplace la confiance. La reconnexion crée une session ou génération neuve.",
        "Test correct, wrong, expired and replaced pairing paths without logging the secret.",
        "Testez les chemins correct, erroné, expiré et remplacé sans journaliser le secret.",
        "A token is secret input; its derived identifiers are still sensitive metadata.",
        "Un jeton est une entrée secrète ; ses identifiants dérivés restent des métadonnées sensibles.",
        ("token", "hash", "e2e"),
    ),
    (
        "advanced.transport-overview", "transport-overview",
        "Direct-first transport overview", "Vue d’ensemble des transports directs",
        "One authenticated application transport owns the PC bridge at a time.",
        "Un seul transport applicatif authentifié possède le pont PC à la fois.",
        "Selection tries a provisioned private-LAN WSS path, opens the E2E relay in signaling standby, retries newly provisioned LAN, attempts data-only WebRTC/P2P, then explicitly commits the encrypted relay. The coordinator and OS-level lease prevent two bridges from delivering one turn.",
        "La sélection essaie le WSS du LAN privé, ouvre le relais E2E en attente de signalisation, retente le LAN nouvellement fourni, essaie WebRTC/P2P pour les données, puis valide explicitement le relais chiffré. Le coordinateur et le verrou système empêchent deux ponts de livrer un tour.",
        "Log the chosen transport and attempt identity, not payload contents.",
        "Journalisez le transport et l’identité de tentative, pas le contenu.",
        "Relay standby is signaling infrastructure until its application commit succeeds.",
        "Le relais en attente ne porte que la signalisation jusqu’à validation applicative.",
        ("lan", "websocket", "webrtc", "p2p", "relay", "e2e"),
    ),
    (
        "advanced.lan", "lan", "LAN transport", "Transport LAN",
        "The local path uses authenticated WSS on a private IPv4 network.",
        "Le chemin local utilise WSS authentifié sur un réseau IPv4 privé.",
        "The PC advertises discovery data, binds the private address on port 8766 and presents a self-signed ECDSA certificate whose SHA-256 pin was provisioned inside the E2E channel. The phone accepts only private numeric addresses and the matching pin. Guest isolation, a public network profile or firewall policy can block discovery or connection.",
        "Le PC annonce la découverte, écoute l’adresse privée sur le port 8766 et présente un certificat ECDSA auto-signé dont l’empreinte SHA-256 a été fournie dans le canal E2E. Le téléphone n’accepte qu’une adresse numérique privée et l’empreinte correspondante. Isolation invité, profil public ou pare-feu peuvent bloquer.",
        "Verify both devices share a trusted private LAN before inspecting fallback transports.",
        "Vérifiez que les deux appareils partagent un LAN privé de confiance avant les replis.",
        "Do not forward the LAN port to the Internet.",
        "Ne redirigez pas le port LAN vers Internet.",
        ("lan", "websocket", "hash"),
    ),
    (
        "advanced.webrtc", "webrtc", "WebRTC and P2P",
        "WebRTC et P2P",
        "WebRTC seeks a direct, ordered and reliable data path across networks.",
        "WebRTC cherche un chemin de données direct, ordonné et fiable entre réseaux.",
        "The PC creates a fresh offer; mobile answers; bounded SDP and ICE candidates travel only inside relay E2E frames. A single ordered DataChannel then performs another pairing-authenticated E2E handshake before application commit. Failed ICE, authentication, timeout or bridge availability closes that attempt and permits relay fallback.",
        "Le PC crée une offre neuve ; le mobile répond ; SDP et candidats ICE bornés voyagent uniquement dans les trames E2E du relais. Un DataChannel ordonné réalise un nouvel échange E2E authentifié avant validation. Échec ICE, authentification, délai ou pont ferme la tentative et permet le relais.",
        "Record offer/answer state, candidate summary and failure category without SDP addresses in shared logs.",
        "Consignez état offre/réponse, résumé des candidats et catégorie d’échec sans adresses SDP dans les journaux partagés.",
        "Every attempt uses fresh generation and cryptographic context.",
        "Chaque tentative utilise une génération et un contexte cryptographique neufs.",
        ("webrtc", "p2p", "sdp", "ice", "signaling", "e2e"),
    ),
    (
        "advanced.ice-stun-turn", "ice-stun-turn", "ICE, STUN and TURN",
        "ICE, STUN et TURN",
        "ICE selects connectivity candidates; STUN discovers mapped addresses; TURN relays WebRTC media or data when direct candidates fail.",
        "ICE sélectionne des candidats de connexion ; STUN découvre les adresses traduites ; TURN relaie les données WebRTC lorsque le direct échoue.",
        "OSIA persists only validated credential-free STUN URLs and currently defaults to Cloudflare STUN. Optional TURN records require credentials from protected runtime environment configuration and are never stored in ordinary settings. TURN is therefore supported as protected configuration, not configured by default; OSIA’s own E2E relay remains the normal fallback.",
        "OSIA ne persiste que des URL STUN validées sans identifiants et utilise par défaut le STUN Cloudflare. Les entrées TURN facultatives exigent des identifiants fournis par l’environnement protégé et ne sont jamais stockées dans les réglages. TURN est donc possible mais absent par défaut ; le relais E2E OSIA reste le repli normal.",
        "Differentiate a STUN reachability failure from a relay failure before changing production configuration.",
        "Distinguez un échec STUN d’un échec du relais avant de changer la production.",
        "Never place TURN credentials in tracked settings or guide examples.",
        "Ne placez jamais d’identifiants TURN dans des réglages suivis ou des exemples.",
        ("ice", "stun", "turn", "webrtc", "relay"),
    ),
    (
        "advanced.relay", "relay", "OSIA relay fallback",
        "Relais de secours OSIA",
        "The public relay becomes the application transport only after direct paths fail and an encrypted commit succeeds.",
        "Le relais public devient le transport applicatif seulement après l’échec des chemins directs et une validation chiffrée.",
        "A Durable Object routes PC and mobile roles for one pair, enforces monotonic sequences, generations, frame and chunk limits, priorities and backpressure, and hibernates idle sockets. Application frames must be E2E envelopes; plaintext is rejected. Duplicate readiness and commits are idempotent, while stale sessions are superseded.",
        "Un Durable Object route les rôles PC/mobile d’une paire, impose séquences monotones, générations, limites de trames/fragments, priorités et pression, puis hiberne les sockets inactifs. Les trames applicatives doivent être E2E ; le clair est refusé. Les doublons sont idempotents et les sessions anciennes remplacées.",
        "Exercise reconnect and duplicate messages in tests before changing relay ordering.",
        "Testez reconnexion et doublons avant de modifier l’ordre du relais.",
        "The relay can route ciphertext and metadata but cannot decrypt application content.",
        "Le relais route chiffré et métadonnées sans pouvoir déchiffrer le contenu.",
        ("relay", "durable-object", "hibernation", "websocket", "e2e", "chunking"),
    ),
    (
        "advanced.e2e", "e2e", "End-to-end encryption",
        "Chiffrement de bout en bout",
        "Endpoint-only key establishment protects confidentiality, integrity and session identity.",
        "L’établissement de clés aux extrémités protège confidentialité, intégrité et identité de session.",
        "PC and mobile combine fresh X25519 agreement with pairing-derived material, derive direction-separated keys and nonces, confirm the transcript, then use authenticated ChaCha20-Poly1305 envelopes. Sequence guards reject replay; authenticated metadata binds role, pair, priority and finality. Oversized ciphertext is bounded, hashed and chunked; reconnect rotates context.",
        "PC et mobile combinent un accord X25519 neuf au matériel dérivé de l’association, dérivent clés et nonces par direction, confirment la transcription puis utilisent des enveloppes ChaCha20-Poly1305 authentifiées. Les séquences bloquent le rejeu ; les métadonnées lient rôle, paire, priorité et finalité. Le chiffré volumineux est borné, haché et fragmenté ; la reconnexion renouvelle le contexte.",
        "Use deterministic cross-language vectors to verify both endpoints without exposing production secrets.",
        "Utilisez des vecteurs déterministes interlangages sans exposer de secrets de production.",
        "Logs may name an error category or shortened key identifier, never key material or plaintext.",
        "Les journaux peuvent nommer une catégorie ou un identifiant raccourci, jamais une clé ni du clair.",
        ("e2e", "hash", "chunking", "token"),
    ),
    (
        "advanced.handshake", "handshake", "Handshake state machine",
        "Machine d’état de négociation",
        "Confirmed E2E identity and transport readiness are separate states.",
        "L’identité E2E confirmée et la disponibilité du transport sont des états distincts.",
        "Relay sessions exchange E2E_HELLO and E2E_READY, then MOBILE_READY and PC_READY describe routable readiness. Direct transports add their own hello, ready and commit sequence tied to attempt and generation. Exact fields, context identity, monotonically ordered messages and duplicate suppression make reconnect deterministic.",
        "Les sessions relais échangent E2E_HELLO et E2E_READY, puis MOBILE_READY et PC_READY décrivent la disponibilité. Les transports directs ajoutent hello, ready et commit liés à la tentative et génération. Champs exacts, identité de contexte, ordre monotone et suppression des doublons rendent la reconnexion déterministe.",
        "Model each transition and reject a frame that arrives before its prerequisite state.",
        "Modélisez chaque transition et refusez une trame arrivée avant son état préalable.",
        "Use only confirmed current message names in documentation and tests.",
        "N’utilisez dans documentation et tests que les noms de messages confirmés.",
        ("e2e", "signaling", "websocket"),
    ),
    (
        "advanced.correlation", "correlation",
        "Conversation and turn correlation", "Corrélation conversation et tour",
        "Stable identities keep concurrent UI events attached to the correct request.",
        "Des identités stables rattachent les événements d’interface à la bonne requête.",
        "Conversation ID selects history; request and turn IDs bind input, attachment batch, progress, tool calls, streamed deltas and terminal event. The active-turn lock blocks overlap. Replay guards and idempotent acknowledgements prevent duplicate delivery, while retry creates an intentional new attempt after the old terminal state.",
        "L’identifiant de conversation choisit l’historique ; requête et tour lient entrée, lot de fichiers, progression, outils, fragments de réponse et événement final. Le verrou bloque le chevauchement. Les gardes anti-rejeu et accusés idempotents évitent les doublons ; Réessayer crée une nouvelle tentative après la fin.",
        "Assert exactly one terminal event and one committed user turn in correlation tests.",
        "Vérifiez exactement un événement final et un tour utilisateur validé dans les tests.",
        "Never infer success from a stale buffered answer.",
        "Ne déduisez jamais un succès d’une ancienne réponse en tampon.",
        ("context",),
    ),
    (
        "advanced.attachments", "attachments", "Attachments",
        "Pièces jointes",
        "Attachments are validated and committed before the agent receives a turn.",
        "Les pièces jointes sont validées et engagées avant que l’agent reçoive le tour.",
        "PC picking creates an ordered draft and validates again at send time. Mobile copies private selections, enforces ten files and a 20 MiB aggregate, emits 20 KiB chunks with size and SHA-256, and commits only after PC reassembly. Shared checks cover MIME, magic bytes, order, cancellation, temporary cleanup and safe names that cannot traverse paths; a transport change aborts the batch.",
        "Sur PC, la sélection crée un brouillon ordonné puis revalide à l’envoi. Le mobile copie en privé, impose dix fichiers et 20 Mio au total, envoie des fragments de 20 Kio avec taille et SHA-256, puis engage après réassemblage PC. Les contrôles couvrent MIME, octets magiques, ordre, annulation, nettoyage et noms sans traversée ; changer de transport annule le lot.",
        "Test a valid mixed batch, wrong hash, duplicate chunk, cancellation and reconnect before commit.",
        "Testez lot mixte valide, mauvais hash, fragment dupliqué, annulation et reconnexion avant validation.",
        "A failed batch must not start the model or replay a previous answer.",
        "Un lot échoué ne doit ni lancer le modèle ni rejouer une réponse précédente.",
        ("chunking", "hash", "mime"),
    ),
    (
        "advanced.vision", "vision", "Vision inputs", "Entrées de vision",
        "Image delivery depends on both adapter format and model capability metadata.",
        "La livraison d’image dépend du format de l’adaptateur et des capacités du modèle.",
        "ChatGPT Account uses its localImage input path; Google and OpenAI-compatible adapters build their supported image content blocks. Provider and model preflight validates MIME and vision support before network work. Unknown capability fails closed, unsupported combinations return a clear error, no silent OCR substitute is invented, and image blocks remain current-turn only.",
        "Compte ChatGPT utilise son chemin localImage ; Google et les adaptateurs compatibles OpenAI construisent leurs blocs d’image pris en charge. Le préflight valide MIME et vision avant le réseau. Une capacité inconnue échoue fermée, une combinaison incompatible donne une erreur claire, aucun OCR silencieux n’est inventé et l’image reste limitée au tour courant.",
        "Run adapter-structure tests with synthetic images and verify persisted history contains no image block.",
        "Testez la structure des adaptateurs avec des images fictives et vérifiez l’absence de bloc image dans l’historique persistant.",
        "Model names alone are not proof of vision support.",
        "Le nom d’un modèle ne prouve pas sa compatibilité vision.",
        ("provider", "model", "mime"),
    ),
    (
        "advanced.provider-architecture", "provider-architecture",
        "Provider architecture", "Architecture des fournisseurs",
        "Adapters normalize different authentication, streaming, tool and image APIs behind one agent contract.",
        "Les adaptateurs normalisent authentification, streaming, outils et images derrière un contrat d’agent commun.",
        "The registry covers ChatGPT Account/App Server, native Google behavior and API-key providers including Anthropic, OpenAI-compatible OpenAI/xAI/DeepSeek paths, Groq and Mistral where exposed. Each adapter owns request shape, streaming parsing, tool support, image encoding and error classification. Quota and retry behavior must preserve the provider’s real status.",
        "Le registre couvre Compte ChatGPT/App Server, Google natif et les fournisseurs à clé exposés, dont Anthropic, les chemins compatibles OpenAI pour OpenAI/xAI/DeepSeek, Groq et Mistral. Chaque adaptateur possède forme de requête, flux, outils, images et classification d’erreurs. Quotas et tentatives doivent préserver l’état réel.",
        "Add adapter tests before advertising a model capability in the registry.",
        "Ajoutez des tests d’adaptateur avant d’annoncer une capacité dans le registre.",
        "Do not generalize one OpenAI-compatible behavior to every provider without evidence.",
        "Ne généralisez pas un comportement compatible OpenAI à tous les fournisseurs sans preuve.",
        ("provider", "api", "model"),
    ),
    (
        "advanced.provider-hierarchy", "provider-hierarchy",
        "Provider and API hierarchy", "Hiérarchie des fournisseurs et API",
        "Automatic hierarchy routes classified work through ordered provider:model entries.",
        "La hiérarchie automatique route le travail classé dans une liste ordonnée fournisseur:modèle.",
        "When enabled, Heavy and Medium chains are tried in user order and the manually selected model is the final fallback. Key rotation precedes model fallback for technical errors. Hierarchy switches only on authentication, quota, timeout, connection or server failures; content/client errors do not switch, and no fallback occurs after output streaming starts. Endpoint fallback is a single transport retry with the same key and model.",
        "Lorsqu’elle est active, les chaînes Lourde et Moyenne suivent l’ordre choisi et le modèle manuel reste le dernier repli. La rotation de clés précède le repli de modèle pour les erreurs techniques. La hiérarchie change seulement pour authentification, quota, délai, connexion ou serveur ; les erreurs de contenu ne changent pas, ni après le début du flux. Le repli d’endpoint est une unique tentative de transport avec même clé et modèle.",
        "Configure two harmless entries, simulate a technical first failure, then verify the second receives no duplicate streamed turn.",
        "Configurez deux entrées sans risque, simulez un premier échec technique et vérifiez l’absence de tour dupliqué.",
        "ChatGPT Account follows its configured position; it is not silently always first.",
        "Compte ChatGPT suit sa position configurée ; il n’est pas secrètement toujours prioritaire.",
        ("provider", "model", "api"),
    ),
    (
        "advanced.model-capabilities", "model-capabilities",
        "Model hierarchy and capabilities", "Hiérarchie et capacités des modèles",
        "The model registry records capabilities used for safe routing and UI controls.",
        "Le registre des modèles décrit les capacités utilisées pour le routage sûr et les contrôles UI.",
        "Metadata identifies provider, reasoning support, vision and tool compatibility. Manual selection is global unless a feature such as autonomous or voice exposes its own selector; conversations do not invent an independent model override. Heavy/Medium hierarchy handles classified tasks, while an unknown image capability fails closed instead of guessing.",
        "Les métadonnées indiquent fournisseur, raisonnement, vision et outils. La sélection manuelle est globale sauf lorsqu’une fonction comme Autonome ou Voix possède son propre sélecteur ; les conversations n’inventent pas de remplacement. La hiérarchie Lourde/Moyenne gère les tâches classées et une capacité image inconnue échoue fermée.",
        "Validate registry metadata, UI availability and adapter behavior as one change.",
        "Validez ensemble métadonnées, disponibilité UI et comportement de l’adaptateur.",
        "Never reintroduce an artificial global response-token ceiling.",
        "Ne réintroduisez jamais un plafond global artificiel de jetons de réponse.",
        ("model", "provider", "thinking", "context"),
    ),
    (
        "advanced.agent-runtime", "agent-runtime", "Agent runtime",
        "Exécution de l’agent",
        "The agent assembles bounded context, calls one provider and executes registered tools until a terminal result.",
        "L’agent assemble un contexte borné, appelle un fournisseur et exécute des outils enregistrés jusqu’au résultat final.",
        "Prompt assembly combines conversation, selected memory, personality/skills, system facts and current attachments. Provider output may stream text and tool calls; parsing occurs before raw tags are stripped, confirmations gate sensitive operations, and anti-loop detectors plus a per-turn time limit stop repeated or stalled behavior. The terminal contract must return useful text or an explicit ERROR state.",
        "L’assemblage combine conversation, mémoire sélectionnée, personnalité/compétences, faits système et pièces jointes courantes. Le fournisseur diffuse texte et appels d’outils ; l’analyse précède le retrait des balises brutes, les confirmations protègent les opérations sensibles, et anti-boucle plus limite de tour arrêtent les répétitions. Le contrat final renvoie un texte utile ou un état ERROR explicite.",
        "Test plain text, native and XML tool calls, denial, loop detection and provider failure without changing protected parser code.",
        "Testez texte, appels natifs et XML, refus, boucle et échec fournisseur sans modifier l’analyseur protégé.",
        "Parsing, execution and stripping order is an architectural contract.",
        "L’ordre analyse, exécution puis retrait est un contrat architectural.",
        ("context", "provider", "model"),
    ),
    (
        "advanced.autonomous", "autonomous", "Autonomous mode internals",
        "Internes du mode autonome",
        "Autonomous mode orchestrates a persisted, bounded mission above normal agent turns.",
        "Le mode autonome orchestre une mission persistante et bornée au-dessus des tours normaux.",
        "The planner creates at most five phases of five tasks, tasks have bounded attempts and the mission has bounded replans. One machine-wide runner writes a crash-safe checkpoint. Interrupt preserves an in-progress state for Resume; Abort marks it terminal and removes the checkpoint. Progress includes planning, task, salvage, failure and terminal events, while each underlying agent turn keeps loop/time protections.",
        "Le planificateur crée au plus cinq phases de cinq tâches, avec tentatives et replanifications bornées. Un exécuteur global écrit un point de reprise sûr. Interrompre conserve l’état pour Reprendre ; Abandonner le rend final et retire le point. La progression couvre plan, tâche, récupération, échec et fin, chaque tour gardant ses protections.",
        "Crash after one task, resume, and verify completed work is rechecked rather than blindly repeated.",
        "Interrompez après une tâche, reprenez et vérifiez que le travail est contrôlé plutôt que répété aveuglément.",
        "Unattended MCP writes remain denied unless the explicit advanced policy permits them.",
        "Les écritures MCP sans surveillance restent refusées sauf politique avancée explicite.",
        ("provider", "model"),
    ),
    (
        "advanced.computer-use", "computer-use", "Computer use internals",
        "Internes du contrôle de l’ordinateur",
        "Computer use escalates from stable integrations to less certain visual interaction.",
        "Le contrôle de l’ordinateur progresse des intégrations stables vers l’interaction visuelle moins certaine.",
        "Priority is API or command integration, URL/deep link, browser DOM and accessible names, native accessibility/UI Automation, keyboard navigation, then screenshot vision and coordinates. Browser sessions use isolated profiles and one managed launch path. Every action should observe resulting process, window, DOM or file state, and sensitive actions pass policy confirmation.",
        "La priorité est API ou commande, URL/lien profond, DOM du navigateur et noms accessibles, accessibilité/UI Automation native, clavier, puis vision et coordonnées. Les sessions navigateur utilisent des profils isolés et un lancement géré. Chaque action observe ensuite processus, fenêtre, DOM ou fichier, et les actions sensibles passent par confirmation.",
        "Prefer a named button in the accessibility tree over a coordinate from an old screenshot.",
        "Préférez un bouton nommé dans l’arbre d’accessibilité à une coordonnée d’ancienne capture.",
        "Coordinate confidence drops when scale, window position or content changes.",
        "La confiance des coordonnées baisse quand échelle, position ou contenu changent.",
        (),
    ),
    (
        "advanced.cron", "cron", "Scheduled tasks and cron",
        "Tâches planifiées et cron",
        "The PC scheduler persists local-wall-clock jobs and polls for due work.",
        "Le planificateur PC persiste des tâches en heure locale et recherche les échéances.",
        "Supported syntax covers every N minutes/hours/days, every N days at a time, daily at a time and an ISO one-shot. Weekly syntax is not supported. One-shots disable after firing; recurring jobs can be edited, toggled or deleted and may bind conversation/model. Startup schedules the next occurrence rather than catching up missed work, so sleep or downtime skips past slots; a busy execution remains retryable.",
        "La syntaxe couvre toutes les N minutes/heures/jours, tous les N jours à une heure, quotidien à une heure et un ISO ponctuel. L’hebdomadaire n’est pas pris en charge. Un ponctuel se désactive ; les récurrents sont modifiables et peuvent lier conversation/modèle. Au démarrage, la prochaine occurrence est calculée sans rattraper les créneaux manqués ; une exécution occupée reste retentable.",
        "Test parsing at timezone and day boundaries with a fake clock, then verify notification and mobile mirror events.",
        "Testez l’analyse aux limites de fuseau et de jour avec une horloge simulée, puis notifications et miroir mobile.",
        "The scheduler cannot wake a sleeping PC by itself.",
        "Le planificateur ne peut pas réveiller seul un PC endormi.",
        ("cron", "model"),
    ),
    (
        "advanced.mcp", "mcp", "MCP architecture", "Architecture MCP",
        "MCP connects external tool servers through catalog or custom configurations.",
        "MCP connecte des serveurs d’outils externes via le catalogue ou des configurations personnalisées.",
        "Supported transports are stdio and streamable HTTP; legacy SSE is recognized but unsupported. Catalog entries define health and auth: OAuth may use Dynamic Client Registration or a static client, bearer tokens are validated, and custom connectors remain explicit. Connected tools are injected as namespaced agent tools. Lifecycle covers connect, cancel, disconnect, revoke and reconnect; mobile mirrors status and confirmation requests.",
        "Les transports pris en charge sont stdio et HTTP streamable ; SSE ancien est reconnu mais non pris en charge. Le catalogue définit santé et auth : OAuth peut utiliser l’enregistrement dynamique ou un client statique, les jetons sont validés et les connecteurs personnalisés explicites. Les outils sont injectés avec namespace. Le cycle couvre connexion, annulation, déconnexion, révocation et reconnexion ; le mobile reflète l’état.",
        "Test a read tool, an annotated write, OAuth cancellation and restart reconnect with sanitized fixtures.",
        "Testez lecture, écriture annotée, annulation OAuth et reconnexion après redémarrage avec données fictives.",
        "Catalog presence, suggestability and current connectability are different states.",
        "Présence au catalogue, proposition et connexion actuelle sont des états différents.",
        ("mcp", "dcr", "oauth", "api"),
    ),
    (
        "advanced.memory-persistence", "memory-persistence",
        "Memory and persistence", "Mémoire et persistance",
        "Conversation history, selected memory and runtime state have separate persistence contracts.",
        "Historique, mémoire sélectionnée et état d’exécution ont des contrats de persistance séparés.",
        "The installed profile uses the resolved user data directory; source mode may use a project config only when its settings file exists. Conversations, memory JSON with atomic backup, settings, ignored API-key and OAuth stores, autonomous checkpoint, cron jobs, LAN identity and guide progress remain PC-owned. Mobile stores pairing plus interface preferences. Migration validates IDs and corruption falls back safely without reopening first-run UI.",
        "Le profil installé utilise le dossier utilisateur résolu ; le mode source peut utiliser config du projet si settings existe. Conversations, mémoire JSON atomique, réglages, magasins ignorés de clés/OAuth, point autonome, tâches, identité LAN et progression restent PC. Le mobile garde association et préférences. La migration valide les ID et la corruption retombe sûrement sans rouvrir le premier lancement.",
        "Test source and installed path resolution with temporary generic directories, never a personal path.",
        "Testez la résolution source/installée avec des dossiers temporaires génériques, jamais un chemin personnel.",
        "Deleting mobile data does not delete PC history or memory.",
        "Supprimer les données mobiles ne supprime pas l’historique ni la mémoire PC.",
        ("context", "token"),
    ),
    (
        "advanced.configuration-secrets", "configuration-secrets",
        "Configuration and secrets", "Configuration et secrets",
        "Configuration is layered so shareable defaults never contain private credentials.",
        "La configuration est organisée en couches afin que les valeurs partageables ne contiennent jamais d’identifiants privés.",
        "Tracked examples document shape and safe defaults; ignored profile files hold real settings, API keys and OAuth state. Android signing keys, Apple signing material, service accounts, R2 credentials and deployment tokens stay outside Git and enter only through approved local files, secret stores or environment variables. Logs, fixtures and screenshots must redact tokens and personal paths.",
        "Les exemples suivis documentent la forme et les valeurs sûres ; les fichiers de profil ignorés contiennent les vrais réglages, clés API et états OAuth. Les clés de signature Android, éléments Apple, comptes de service, identifiants R2 et jetons de déploiement restent hors de Git et passent uniquement par des fichiers locaux approuvés, magasins secrets ou variables d’environnement. Journaux, fixtures et captures doivent masquer jetons et chemins personnels.",
        "Use a placeholder in an example file, inject a disposable test secret locally, then inspect the staged diff before committing.",
        "Placez un substitut dans le fichier d’exemple, injectez localement un secret de test jetable, puis inspectez le diff indexé avant validation.",
        "A masked field is a display safeguard, not proof that no raw secret exists in authenticated runtime memory.",
        "Un champ masqué protège l’affichage ; il ne prouve pas qu’aucun secret brut n’existe dans la mémoire d’exécution authentifiée.",
        ("api", "oauth", "r2"),
    ),
    (
        "advanced.development-environment", "development-environment",
        "Development environment", "Environnement de développement",
        "Reproducible development starts with the repository’s documented toolchain and isolated dependencies.",
        "Un développement reproductible commence par la chaîne d’outils documentée du dépôt et des dépendances isolées.",
        "PC work uses PowerShell, a supported Python virtual environment and Qt dependencies; mobile uses the validated Flutter/Dart toolchain with Android SDK, Java and Gradle; Worker and site tooling use their declared Node packages and Wrangler where applicable. Resolve paths dynamically, keep generated caches out of source control, and record tool versions in release evidence rather than hard-coding them into timeless guidance.",
        "Le PC utilise PowerShell, un environnement virtuel Python pris en charge et les dépendances Qt ; le mobile utilise la chaîne Flutter/Dart validée avec SDK Android, Java et Gradle ; Worker et site suivent leurs paquets Node déclarés et Wrangler si nécessaire. Résolvez les chemins dynamiquement, excluez les caches générés du suivi et consignez les versions dans les preuves de livraison plutôt que dans un guide intemporel.",
        "From a fresh clone, create the prescribed environments, restore locked dependencies and run the smallest import or analysis check before editing.",
        "Depuis un clone neuf, créez les environnements prescrits, restaurez les dépendances verrouillées et lancez le plus petit test d’import ou d’analyse avant toute modification.",
        "A tool installed globally can hide a missing repository dependency.",
        "Un outil installé globalement peut masquer une dépendance manquante du dépôt.",
        ("api",),
    ),
    (
        "advanced.takeover-workflow", "takeover-workflow",
        "Twelve-step takeover workflow", "Reprise d’un travail en douze étapes",
        "A disciplined takeover makes repository state, running services and release evidence explicit before any edit.",
        "Une reprise disciplinée rend explicites l’état des dépôts, les services actifs et les preuves de livraison avant toute modification.",
        "1. Read the request and repository rules. 2. Record every repository path and allowed scope. 3. Capture HEAD, branch, status, index and upstream divergence. 4. Classify existing changes as user-owned or task-owned. 5. Audit relevant processes and services. 6. Inspect architecture and all call sites. 7. Build a source-backed behavior matrix. 8. Define the smallest cross-platform change. 9. Implement without touching protected contracts. 10. Run syntax, import, focused and adjacent tests. 11. Perform an adversarial correctness, regression and policy review. 12. Stage explicitly, verify the exact diff, commit and build from the committed snapshot.",
        "1. Lire la demande et les règles. 2. Noter chaque dépôt et le périmètre autorisé. 3. Relever HEAD, branche, état, index et divergence. 4. Classer les changements existants. 5. Auditer processus et services. 6. Étudier architecture et appelants. 7. Établir une matrice sourcée. 8. Définir le changement multiplateforme minimal. 9. Implémenter sans toucher aux contrats protégés. 10. Tester syntaxe, imports, cible et voisinage. 11. Mener une revue adversariale. 12. Indexer explicitement, vérifier le diff, valider et construire depuis le commit.",
        "Practice on a disposable clone at C:\\path\\to\\osia and stop if a protected or user-owned change overlaps the task.",
        "Exercez-vous sur un clone jetable dans C:\\path\\to\\osia et arrêtez-vous si un changement protégé ou appartenant à l’utilisateur chevauche la tâche.",
        "A clean-looking worktree does not replace recording the commit and upstream relationship.",
        "Un arbre apparemment propre ne remplace pas le relevé du commit et de sa relation avec l’amont.",
        ("artifact", "rollout"),
    ),
    (
        "advanced.safe-modification", "safe-modification",
        "Safe modification workflow", "Flux de modification sûr",
        "Safe changes begin with evidence, preserve contracts and finish with an exact staged diff.",
        "Les changements sûrs commencent par des preuves, préservent les contrats et finissent par un diff indexé exact.",
        "Reproduce or trace the behavior, identify root cause and every caller, then edit only necessary functions. Preserve parser order, anti-loop, key rotation, configuration resolution, WebSocket contracts and platform guards. Never clean a shared tree with destructive reset. Run targeted and adjacent checks, inspect generated artifacts physically, review from correctness, regression and instruction angles, then stage named paths and compare index against intent.",
        "Reproduisez ou tracez le comportement, trouvez la cause et tous les appelants, puis ne modifiez que les fonctions nécessaires. Préservez ordre d’analyse, anti-boucle, rotation de clés, résolution de configuration, contrats WebSocket et gardes de plateforme. Ne nettoyez jamais un arbre partagé par réinitialisation destructive. Testez cible et voisinage, inspectez physiquement les artefacts, menez la revue multi-angle, indexez les chemins nommés et comparez l’index à l’intention.",
        "For a two-file fix, stage those two paths explicitly and prove no third path appears in the cached diff.",
        "Pour un correctif à deux fichiers, indexez explicitement ces deux chemins et prouvez qu’aucun troisième n’apparaît dans le diff indexé.",
        "Passing tests do not authorize unrelated cleanup or refactoring.",
        "Des tests réussis n’autorisent ni nettoyage ni refactoring hors périmètre.",
        ("hash", "artifact"),
    ),
    (
        "advanced.testing-strategy", "testing-strategy",
        "Testing strategy", "Stratégie de test",
        "OSIA testing combines fast contract tests with physical checks at system boundaries.",
        "Les tests d’OSIA combinent contrats rapides et contrôles physiques aux frontières du système.",
        "PC changes need syntax/import checks plus focused and adjacent pytest suites; Flutter changes need formatting, static analysis, unit/widget tests and release builds. Worker and site changes use their declared checks. Transport and end-to-end tests cover authentication, sequencing, disconnects, replay, fallback and E2E opacity with deterministic fakes. Attachment tests cover limits, hashes, MIME, cancellation and atomic commit. Frozen-app, package and security checks validate the actual artifact, not merely source behavior.",
        "Les changements PC exigent syntaxe/imports puis tests pytest ciblés et voisins ; Flutter exige formatage, analyse statique, tests unitaires/widgets et builds release. Worker et site suivent leurs contrôles déclarés. Les tests transport et bout en bout couvrent authentification, séquences, déconnexions, rejeu, repli et opacité E2E avec des doublures déterministes. Les pièces jointes couvrent limites, hash, MIME, annulation et validation atomique. Les contrôles frozen, paquet et sécurité valident l’artefact réel.",
        "Inject a fake clock and transport to prove one terminal event, then repeat the critical path on the packaged binary.",
        "Injectez une horloge et un transport fictifs pour prouver un seul événement final, puis rejouez le chemin critique sur le binaire empaqueté.",
        "A mocked source test cannot prove signing, native libraries or installer contents.",
        "Un test source simulé ne prouve ni signature, ni bibliothèques natives, ni contenu de l’installateur.",
        ("e2e", "artifact", "hash"),
    ),
    (
        "advanced.windows-packaging", "windows-packaging",
        "Windows packaging", "Empaquetage Windows",
        "A Windows release is produced from a clean committed snapshot and validated at every packaging layer.",
        "Une version Windows est produite depuis un instantané validé propre et contrôlée à chaque couche d’empaquetage.",
        "The repository build creates a PyInstaller onedir application, runs package smoke checks and validates GUI startup before an optional Inno Setup installer. Release metadata and SHA-256 must match immutable artifacts; code signing is a separate authenticity layer. The updater must consume the intended channel manifest and verify size/hash before replacement. Build directories are evidence until the new artifact is independently validated.",
        "Le build du dépôt crée une application PyInstaller onedir, exécute des tests du paquet et valide le démarrage GUI avant un éventuel installateur Inno Setup. Métadonnées et SHA-256 doivent correspondre aux artefacts immuables ; la signature de code est une couche d’authenticité distincte. Le programme de mise à jour doit lire le manifeste du canal prévu et vérifier taille/hash avant remplacement. Les dossiers de build restent des preuves jusqu’à validation indépendante du nouvel artefact.",
        "Launch the packaged executable from outside the source tree, exercise a harmless chat/settings path and compare its digest with the manifest candidate.",
        "Lancez l’exécutable empaqueté hors de l’arbre source, testez un chemin conversation/réglages sans risque et comparez son empreinte au manifeste candidat.",
        "Do not publish or delete the last known-good build before physical validation succeeds.",
        "Ne publiez ni ne supprimez le dernier build valide avant la réussite du contrôle physique.",
        ("pyinstaller", "inno-setup", "manifest", "hash", "artifact"),
    ),
    (
        "advanced.android-packaging", "android-packaging",
        "Android packaging", "Empaquetage Android",
        "Android releases bind Flutter output, version metadata, signing and store artifacts into one auditable result.",
        "Les versions Android relient sortie Flutter, métadonnées de version, signature et artefacts de store en un résultat vérifiable.",
        "Record the validated Flutter version with the build evidence. Increment versionName for the user release and versionCode for every uploaded build. Use release signing from ignored local configuration, build APK and AAB with production defines, inspect the bundle with bundletool, and verify application ID, minimum/target SDK and ABIs. R8 output includes a mapping to retain; keep native symbols when generated. Play App Signing protects the distribution key while the upload key signs submissions promoted deliberately through internal, closed, open and production tracks.",
        "Consignez la version Flutter validée avec les preuves du build. Incrémentez versionName pour la version utilisateur et versionCode pour chaque envoi. Utilisez la signature release depuis une configuration locale ignorée, construisez APK et AAB avec les paramètres production, inspectez le bundle avec bundletool, puis vérifiez identifiant, SDK minimal/cible et ABI. Conservez le mapping R8 et les symboles natifs générés. Play App Signing protège la clé de distribution tandis que la clé d’envoi signe les soumissions promues consciemment entre canaux interne, fermé, ouvert et production.",
        "Install the signed release APK on a compatible device, validate pairing and reconnection, then inspect the AAB before any store upload.",
        "Installez l’APK release signé sur un appareil compatible, validez association et reconnexion, puis inspectez l’AAB avant tout envoi au store.",
        "Never print or copy keystore passwords into logs, commands committed to Git or release notes.",
        "N’affichez ni ne copiez les mots de passe du keystore dans les journaux, commandes suivies ou notes de version.",
        ("version-name", "version-code", "artifact", "hash", "rollout"),
    ),
    (
        "advanced.worker-site-deployment", "worker-site-deployment",
        "Worker and site deployment", "Déploiement Worker et site",
        "Worker, object storage and public-site updates are separate deployments joined by channel metadata.",
        "Worker, stockage objet et site public sont des déploiements séparés reliés par les métadonnées de canal.",
        "Validate Worker configuration, bindings, Durable Object migrations and compatibility before deployment. Upload immutable artifacts to R2 under versioned keys, then publish a small internal-preview or stable channel manifest with version, URL, size and SHA-256 only after verification. osia.space presents authoritative availability and release metadata without claiming platforms or channels that are not public. Rollback restores a known-good Worker or manifest pointer; it never overwrites an immutable version.",
        "Validez configuration Worker, bindings, migrations Durable Object et compatibilité avant déploiement. Envoyez les artefacts immuables dans R2 sous des clés versionnées, puis publiez le manifeste du canal aperçu interne ou stable avec version, URL, taille et SHA-256 seulement après vérification. osia.space présente disponibilité et métadonnées réelles sans annoncer de plateforme ou canal non public. Le retour arrière restaure un Worker connu ou un pointeur de manifeste ; il ne remplace jamais une version immuable.",
        "Deploy to a non-production environment, verify rendezvous and blind relay behavior, then check the exact manifest URL from a clean client.",
        "Déployez dans un environnement hors production, vérifiez rendez-vous et relais aveugle, puis contrôlez l’URL exacte du manifeste depuis un client propre.",
        "Repository access does not imply authorization to deploy or publish.",
        "L’accès au dépôt n’implique pas l’autorisation de déployer ou publier.",
        ("durable-object", "r2", "manifest", "rollout", "artifact"),
    ),
    (
        "advanced.release-process", "release-process",
        "Release process", "Processus de livraison",
        "Release order prevents metadata from pointing at an artifact that is missing or unverified.",
        "L’ordre de livraison empêche les métadonnées de pointer vers un artefact absent ou non vérifié.",
        "Audit and prepare versions, run all tests, then create targeted local commits and physical-review builds. After approval, fetch and recheck divergence, push normally, build final artifacts from the pushed commits, and perform a physical update test. Upload immutable files, update the channel manifest, then update osia.space metadata. Promote Android through Google Play testing separately; iOS/TestFlight follows its own signing and review process. Monitor rollout and prune only superseded completed evidence while keeping the known-good and ambiguous builds.",
        "Auditez et préparez les versions, lancez tous les tests, puis créez les commits locaux ciblés et les builds de revue physique. Après approbation, faites fetch et revérifiez la divergence, poussez normalement, construisez les artefacts finaux depuis les commits poussés et testez physiquement la mise à jour. Envoyez les fichiers immuables, actualisez le manifeste puis les métadonnées osia.space. Promouvez Android séparément via les tests Google Play ; iOS/TestFlight suit son propre processus de signature et revue. Surveillez le déploiement et ne supprimez que les preuves terminées remplacées.",
        "Use a written checklist with one evidence path or digest beside every completed gate.",
        "Utilisez une liste écrite avec un chemin de preuve ou une empreinte pour chaque jalon terminé.",
        "A successful local build is a candidate, not a published release.",
        "Un build local réussi est un candidat, pas une version publiée.",
        ("manifest", "hash", "artifact", "rollout"),
    ),
    (
        "advanced.security-model", "security-model",
        "Security model", "Modèle de sécurité",
        "OSIA combines local ownership, authenticated transports, least privilege and explicit confirmation at sensitive boundaries.",
        "OSIA combine propriété locale, transports authentifiés, moindre privilège et confirmation explicite aux frontières sensibles.",
        "Pairing establishes mobile trust, device identity and session keys; encrypted transports protect content, while a blind Worker relay routes ciphertext without plaintext. Secrets stay in ignored stores or approved bindings, logs are redacted, files are validated, and replay/sequence guards reject stale traffic. Tools are allowlisted and risky actions require confirmation; unattended connectors are stricter. Signed metadata, size and SHA-256 protect update integrity. External AI providers still receive content required for configured requests under their own policies.",
        "L’association établit confiance mobile, identité et clés de session ; les transports chiffrés protègent le contenu, tandis qu’un relais Worker aveugle route le chiffré sans texte clair. Les secrets restent dans des magasins ignorés ou bindings approuvés, les journaux sont masqués, les fichiers validés et les gardes de rejeu/séquence refusent l’ancien trafic. Les outils sont autorisés explicitement et les actions risquées demandent confirmation. Métadonnées signées, taille et SHA-256 protègent l’intégrité des mises à jour. Les fournisseurs IA externes reçoivent néanmoins le contenu nécessaire selon leurs règles.",
        "Threat-model a stolen pairing token, replayed frame, malicious filename, compromised provider key and untrusted MCP write separately.",
        "Modélisez séparément vol de jeton d’association, trame rejouée, nom malveillant, clé fournisseur compromise et écriture MCP non fiable.",
        "Encryption in transit does not replace endpoint security, backups or provider governance.",
        "Le chiffrement en transit ne remplace ni sécurité des terminaux, ni sauvegardes, ni gouvernance fournisseur.",
        ("e2e", "ciphertext", "hkdf", "nonce", "oauth", "mcp"),
    ),
    (
        "advanced.developer-troubleshooting", "developer-troubleshooting",
        "Developer troubleshooting", "Dépannage développeur",
        "Troubleshooting follows the failing boundary from observable symptom to the smallest reproducible contract.",
        "Le dépannage suit la frontière défaillante depuis le symptôme observable jusqu’au plus petit contrat reproductible.",
        "For startup, distinguish a port conflict, stale process, wrong profile, missing frozen import or missing PyInstaller asset. For mobile, separate pairing mismatch, WebSocket disconnect, ICE or STUN failure, expected relay fallback and a duplicate handshake. For Android, check app-signature mismatch and versionCode conflict. For tests, identify developer-profile dependence and any runtime file appearing unexpectedly in source. For providers, distinguish image rejection from authentication/quota; for schedules, verify syntax, due time and that the PC was running. Sanitize logs and preserve evidence before cleanup.",
        "Au démarrage, distinguez conflit de port, processus obsolète, mauvais profil, import frozen manquant ou asset PyInstaller absent. Pour le mobile, séparez association incohérente, déconnexion WebSocket, échec ICE ou STUN, repli normal vers le relais et handshake dupliqué. Pour Android, vérifiez signature d’app et conflit versionCode. Pour les tests, repérez la dépendance au profil développeur et tout fichier d’exécution apparu dans les sources. Pour les fournisseurs, distinguez rejet d’image et auth/quota ; pour les tâches, vérifiez syntaxe, échéance et PC actif. Masquez les journaux et conservez les preuves.",
        "Reduce a reconnect failure to one synthetic request ID and timeline before changing retry code.",
        "Réduisez un échec de reconnexion à un identifiant de requête et une chronologie fictifs avant de modifier les tentatives.",
        "Do not treat a retry that hides duplication or data loss as a fix.",
        "Ne considérez pas comme correctif une nouvelle tentative qui masque duplication ou perte de données.",
        ("request-id", "sequence-number", "hash", "artifact"),
    ),
    (
        "advanced.glossary", "glossary", "Glossary", "Glossaire",
        "Stable terms make the guide, UI, protocol tests and release evidence easier to compare.",
        "Des termes stables facilitent la comparaison entre guide, interface, tests de protocole et preuves de livraison.",
        "Provider: service exposing models; model: selected AI implementation; API: programmatic service contract; token: bounded credential or model text unit, by context; context: input retained for a model turn; thinking: supported reasoning effort; MCP: Model Context Protocol; DCR: OAuth dynamic client registration; OAuth: delegated authorization; cron: schedule expression; WebSocket: persistent bidirectional connection.\n\nE2E: endpoint-to-endpoint encryption; LAN: local area network; P2P: direct peer-to-peer path; WebRTC: real-time peer transport; SDP: WebRTC session description; ICE: WebRTC connectivity process; STUN: public-path discovery; TURN: WebRTC relay service; signaling: offer, answer and candidate exchange.\n\nRelay: routed fallback path; Durable Object: stateful Worker coordination unit; hibernation: suspended object with resumable sockets; chunking: splitting a file for transport; hash: integrity digest; MIME: declared media type; ABI: native application binary interface; APK: installable Android package; AAB: Android App Bundle for stores; R8: Android shrinker and obfuscator.\n\nPyInstaller: Python application packager; Inno Setup: Windows installer builder; R2: object storage; rollout: staged release exposure; versionName: Android user version; versionCode: Android upload integer.",
        "Fournisseur : service exposant des modèles ; modèle : implémentation IA choisie ; API : contrat programmable ; token : identifiant borné ou unité de texte selon le contexte ; contexte : entrée d’un tour ; thinking : effort de raisonnement pris en charge ; MCP : Model Context Protocol ; DCR : enregistrement dynamique OAuth ; OAuth : autorisation déléguée ; cron : expression de planification ; WebSocket : connexion bidirectionnelle persistante.\n\nE2E : chiffrement de bout en bout ; LAN : réseau local ; P2P : chemin direct entre pairs ; WebRTC : transport temps réel entre pairs ; SDP : description de session WebRTC ; ICE : recherche de connectivité ; STUN : découverte du chemin public ; TURN : relais WebRTC ; signalisation : échange offre, réponse et candidats.\n\nRelais : chemin de repli routé ; Durable Object : unité Worker à état ; hibernation : objet suspendu avec sockets reprenables ; chunking : découpage pour transport ; hash : empreinte d’intégrité ; MIME : type de média déclaré ; ABI : interface binaire native ; APK : paquet Android installable ; AAB : Android App Bundle pour les stores ; R8 : réduction et obfuscation Android.\n\nPyInstaller : empaqueteur Python ; Inno Setup : créateur d’installateur Windows ; R2 : stockage objet ; rollout : exposition progressive ; versionName : version Android visible ; versionCode : entier Android d’envoi.",
        "When a protocol field and UI label differ, document the mapping instead of silently using both words as synonyms.",
        "Si un champ protocolaire et un libellé UI diffèrent, documentez la correspondance au lieu d’en faire discrètement des synonymes.",
        "Definitions describe OSIA’s usage; external standards remain authoritative for their specifications.",
        "Ces définitions décrivent l’usage d’OSIA ; les normes externes restent l’autorité pour leurs spécifications.",
        _GLOSSARY_IDS,
    ),
)


_essential_lessons = _ORIGINAL_LESSONS[:8]
_legacy_mastery_lessons = tuple(
    replace(
        lesson,
        category=CATEGORY_MASTERY,
        order=_LEGACY_MASTERY_METADATA[lesson.id][0],
        subject_id=_LEGACY_MASTERY_METADATA[lesson.id][1],
        title={
            "en": _LEGACY_MASTERY_METADATA[lesson.id][2],
            "fr": _LEGACY_MASTERY_METADATA[lesson.id][3],
        },
    )
    for lesson in _ORIGINAL_LESSONS[8:]
)
_new_mastery_lessons = tuple(
    _curriculum_lesson(CATEGORY_MASTERY, order, spec)
    for order, spec in (
        (next_order, spec)
        for next_order in range(1, 21)
        for spec in _MASTER_NEW_SPECS
        if {
            "mental-model": 1,
            "conversations-turns": 2,
            "providers-accounts": 3,
            "reasoning": 6,
            "keys-quotas-security": 7,
            "files-images": 8,
            "autonomous": 9,
            "connectors": 11,
            "tools-computer-control": 13,
            "notifications-background": 16,
            "settings-tour": 17,
            "troubleshooting": 19,
        }.get(spec[1]) == next_order
    )
)
_mastery_lessons = tuple(
    sorted((*_legacy_mastery_lessons, *_new_mastery_lessons), key=lambda item: item.order)
)
_advanced_lessons = tuple(
    _curriculum_lesson(CATEGORY_ADVANCED, order, spec)
    for order, spec in enumerate(_ADVANCED_SPECS, start=1)
)
LESSONS = (*_essential_lessons, *_mastery_lessons, *_advanced_lessons)


LESSONS_BY_ID = {lesson.id: lesson for lesson in LESSONS}


def lessons_for_category(category: str) -> tuple[GuideLesson, ...]:
    return tuple(
        sorted(
            (lesson for lesson in LESSONS if lesson.category == category),
            key=lambda lesson: lesson.order,
        )
    )


__all__ = [
    "CATEGORIES",
    "CATEGORY_ADVANCED",
    "CATEGORY_ESSENTIAL",
    "CATEGORY_MASTERY",
    "GUIDE_CONTENT_SCHEMA_VERSION",
    "LESSONS",
    "LESSONS_BY_ID",
    "GuideCategory",
    "GuideLesson",
    "lessons_for_category",
    "localized",
]

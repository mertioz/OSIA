"""Offline, bilingual content model for the integrated OSIA learning guide."""

from __future__ import annotations

from dataclasses import dataclass


GUIDE_CONTENT_SCHEMA_VERSION = 2
CATEGORY_ESSENTIAL = "getting-started"
CATEGORY_ADVANCED = "master-osia"


Localized = dict[str, str]


@dataclass(frozen=True)
class GuideCategory:
    id: str
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


def localized(value: Localized | None, language: str) -> str:
    """Resolve guide copy in ``language``, falling back to English."""

    if not value:
        return ""
    return value.get(language) or value.get("en") or ""


CATEGORIES = (
    GuideCategory(
        id=CATEGORY_ESSENTIAL,
        order=1,
        title={"en": "Getting started", "fr": "Démarrage essentiel"},
        summary={
            "en": "The essentials for a confident first conversation, with no technical knowledge required.",
            "fr": "L’essentiel pour commencer avec confiance, sans connaissances techniques.",
        },
    ),
    GuideCategory(
        id=CATEGORY_ADVANCED,
        order=2,
        title={"en": "Master OSIA", "fr": "Maîtriser OSIA"},
        summary={
            "en": "A deeper, plain-language guide to connections, voice, models, memory and safe automation.",
            "fr": "Un guide approfondi et clair sur les connexions, la voix, les modèles, la mémoire et l’automatisation sûre.",
        },
    ),
)


LESSONS = (
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
    "GUIDE_CONTENT_SCHEMA_VERSION",
    "LESSONS",
    "LESSONS_BY_ID",
    "GuideCategory",
    "GuideLesson",
    "lessons_for_category",
    "localized",
]

# Proposed OSIA privacy-policy update — Personal Browser

## Audit result

The public page at `https://osia.space/legal/privacy` returned HTTP 200 on 2026-08-12, but it is explicitly marked `2026-07-14-draft.1`, “not approved or in force,” and `noindex, nofollow`. It does not describe the Personal Browser extension, explicit tab authorization, or Native Messaging lifecycle. It is therefore not yet an adequate production store privacy-policy source.

The exact update point is after current section **13. Outils et contrôle de l'ordinateur**. Add the dedicated section below, then make the following related edits:

- section **23. Catégories de destinataires**: clarify that Personal Browser has no extension-operated server, Worker, or relay browser protocol, while necessary tool observations may reach the AI provider selected in OSIA;
- section **27. Durées de conservation**: add installation identifier, browser-session identifier, exact-tab authorization metadata, and the non-persistence of page/form content by the extension;
- section **29. Contrôles et effacement**: add exact-tab revoke, cross-origin revoke, browser/OSIA restart clearing, removal of extension data, and uninstall;
- section **35. Date d'effet et version**: issue a new reviewed version before either store submission.

The final public policy must be accessible, accurate, approved for publication, and consistent with both store dashboards. Do not use the current draft URL as a production disclosure until that gate is resolved.

## Proposed English section

### Personal Browser extension and explicit tab access

OSIA Personal Browser is an optional Chromium extension used with the locally installed OSIA Desktop application. The user must click the extension button in the exact current tab to authorize it, and must separately select Personal Browser for the current OSIA turn. Ordinary browser turns continue to use OSIA's isolated browser. Authorization does not silently spread to sibling or duplicated tabs, popups, other windows, or future turns.

For an explicitly authorized, non-sensitive tab, the extension can process the page origin and path without query or fragment, title, bounded visible text, interactive-element metadata, non-sensitive form metadata, and a non-sensitive form value supplied for the requested operation. The extension sends this information to OSIA Desktop through same-user local Native Messaging. The extension does not connect to an extension-operated network service and does not use a Worker or relay as a browser protocol. Depending on the model selected in OSIA, Desktop may include observations needed for the turn in context sent to that AI provider; a local model may keep that model processing on the PC. The provider's terms and OSIA's general provider disclosures also apply.

The extension does not request or extract browser cookies, saved passwords, authentication cookies, browsing history, or browser profile databases. It has no `<all_urls>` permission, analytics, advertising, remote hosted code, or continuous background browsing surveillance. Password, one-time-code, login, authentication, payment, checkout, browser-internal, and extension-store surfaces are blocked or left for manual handling.

The extension stores a random installation identifier locally until extension data is removed. Browser-session and exact-tab authorization metadata remain in session storage only for the active browser session. Page content and form values are not persisted by the extension. Authorization ends on explicit revoke, cross-origin navigation, Native Messaging host loss, browser or OSIA restart, extension-data removal, or uninstall. The extension's data use is limited to providing the disclosed Personal Browser feature and follows the Chrome Web Store User Data Policy and its Limited Use requirements; it is not used or shared for advertising, tracking, data brokerage, credit decisions, or sale.

## Proposition de section française

### Extension Personal Browser et accès explicite à un onglet

OSIA Personal Browser est une extension Chromium facultative utilisée avec l'application OSIA Desktop installée localement. L'utilisateur doit cliquer sur le bouton de l'extension dans l'onglet courant exact pour l'autoriser, puis sélectionner séparément Personal Browser pour le tour OSIA en cours. Les tours de navigation ordinaires continuent d'utiliser le navigateur isolé d'OSIA. L'autorisation ne s'étend pas silencieusement aux onglets voisins ou dupliqués, aux fenêtres contextuelles, aux autres fenêtres ou aux tours futurs.

Pour un onglet non sensible explicitement autorisé, l'extension peut traiter l'origine et le chemin de la page sans requête ni fragment, son titre, un texte visible borné, les métadonnées des éléments interactifs, les métadonnées des formulaires non sensibles et une valeur de formulaire non sensible fournie pour l'opération demandée. L'extension transmet ces informations à OSIA Desktop par Native Messaging local limité au même utilisateur. Elle ne se connecte à aucun service réseau exploité par l'extension et n'utilise ni Worker ni relais comme protocole navigateur. Selon le modèle choisi dans OSIA, Desktop peut inclure les observations nécessaires au tour dans le contexte envoyé à ce fournisseur IA ; un modèle local peut conserver ce traitement du modèle sur le PC. Les conditions du fournisseur et les informations générales d'OSIA sur les fournisseurs s'appliquent également.

L'extension ne demande ni n'extrait les cookies du navigateur, les mots de passe enregistrés, les cookies d'authentification, l'historique de navigation ou les bases de données du profil. Elle ne possède aucune permission `<all_urls>`, aucun outil d'analyse ou de publicité, aucun code hébergé à distance et aucune surveillance continue de la navigation en arrière-plan. Les mots de passe, codes à usage unique, surfaces de connexion, d'authentification, de paiement, de validation de commande, internes au navigateur et des boutiques d'extensions sont bloqués ou laissés à une intervention manuelle.

L'extension conserve localement un identifiant d'installation aléatoire jusqu'à la suppression de ses données. Les métadonnées de session navigateur et d'autorisation de l'onglet exact restent uniquement dans le stockage de session pendant la session active du navigateur. Le contenu des pages et les valeurs de formulaire ne sont pas conservés par l'extension. L'autorisation prend fin lors d'une révocation explicite, d'une navigation vers une autre origine, de la perte de l'hôte Native Messaging, d'un redémarrage du navigateur ou d'OSIA, de la suppression des données de l'extension ou de sa désinstallation. L'utilisation des données par l'extension est limitée à la fourniture de la fonctionnalité Personal Browser décrite et respecte la politique relative aux données utilisateur du Chrome Web Store et ses exigences d'utilisation limitée ; aucune donnée n'est utilisée ni partagée pour la publicité, le suivi, le courtage de données, une décision de crédit ou la vente.

# i18n migration backlog

OSIA's PC GUI now has a centralized i18n foundation: `osia/i18n.py`
(`t(key)`, `set_language()`, `add_listener()`), a `language` setting in
`config/settings.json` (default `en`), and a Language selector in Settings.
Supported languages: **en, fr, zh, ja, de, es**.

The **newly-added / changed** strings are already routed through `i18n.t()`:

- Provider configuration card: `provider.get_key`, `provider.browse`,
  `provider.key_placeholder`, `provider.save`, `provider.saved`,
  `provider.no_key_required`.
- Language selector: `settings.language`, `settings.language_sub`.

## Not yet migrated (follow-up pass)

These existing strings are still hardcoded (a mix of EN and FR). Each needs a
key in `osia/i18n.TRANSLATIONS` (all 6 languages) and a `t()` call at the
site, plus a `retranslate()`/listener hook where the widget persists. File:
`osia/gui/main_popup.py` unless noted.

### Chat / input
- `:170` "Say "OSIA" or type below to start."
- `:187` "Attach a file" (tooltip)
- `:192` "Message OSIA…" (placeholder)
- `:199` "Send" (tooltip)

### Conversations panel
- `:362` "Conversations"
- `:402` "No conversations yet."

### Model panel
- `:531` / `:590` "Models"
- `:538` / `:591` "Choose a provider to see its models."
- `:620` "Pick the model OSIA will use for new messages."
- `:629` "No models available for this provider."

### Settings — header & sections
- `:811` "Settings"
- `:817` "Minimize" (tooltip)
- `:903` "Smart model selection"
- `:913` "Auto-pick a model per prompt."
- `:925` "Démarrage"  *(FR)*
- `:927` "Lancer le serveur mobile et le tunnel Cloudflare au démarrage."  *(FR)*
- startup row labels "Serveur mobile" / "Tunnel Cloudflare"  *(FR)*
- `:977` / `:1003` "Voice"
- `:979` "Choose how OSIA speaks. Changes apply immediately."
- `:986` "Provider"
- `:1025` "AI providers"
- `:1027` "Choose a provider and configure its API key."
- "Choose provider  ▾" button
- Voice combo prefixes "French — " / "English — "

### Notes
- Line numbers drift as the file changes; grep the literal text.
- `osia/gui/dynamic_island.py` has no translatable strings (only the "OSIA"
  wordmark, a brand name).
- Follow the pattern in `_ProviderCard`: build with `i18n.t(...)`, add a
  `_retranslate()` method, register it with `i18n.add_listener()` in
  `__init__`, and unregister on `destroyed`.

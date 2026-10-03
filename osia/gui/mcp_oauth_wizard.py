#!/usr/bin/env python3
"""Guided Google OAuth setup wizard for BYO Google clients.

Google Drive MCP and native Gmail REST both use a PRE-REGISTERED OAuth client
(model B, per user): the user creates a Google OAuth client and pastes its
``client_id`` + ``client_secret`` into the connector card
(see ``main_popup._build_mcp_card``). That Google Cloud setup is ~15-30 min and
has two mode-specific gotchas: Drive MCP still needs Google's MCP-specific
``drivemcp`` API, while native Gmail REST needs the classic Gmail API
(``gmail.googleapis.com``). Drive uses a Desktop client; Gmail uses a Web client
with both the PC loopback and stable Mobile rendezvous redirects registered.
This dialog walks the user through it in 5 numbered steps.

Design (audited)
----------------
* A SEPARATE top-level ``QDialog`` — it is NOT a child of the popup's layout, so it
  cannot touch the popup's protected sizing (``POPUP_W`` / ``sizeHint`` /
  ``_animate_width`` / ``_CentralStack``). The popup only adds one gated button that
  exec()s this dialog.
* INSTRUCTIONAL ONLY — the wizard never writes the ``client_id`` / ``client_secret``
  fields nor any connect/persist path. The user still pastes manually.
* Uses the popup's protected frameless dialog chrome: frameless +
  ``dialogCard`` object name (inherits the popup's premium stylesheet),
  ``FramelessWindowHint | Dialog | WindowStaysOnTopHint`` (so the modal surfaces
  above the always-on-top popup on X11/Wayland), ``WA_TranslucentBackground``,
  ``setModal(True)`` and a ``showEvent`` that centres on the parent window.
  OSIA is permanently full-access: no permission or confirmation prompt gates
  setup.
* Imports PyQt6 + ``osia.i18n`` (+ stdlib ``webbrowser``), and lazily imports
  ``google_oauth`` only to display the stable Mobile redirect. No GUI module is
  pulled back in, so the dependency remains acyclic.

URLs live HERE (code), never in i18n — links are not translated and must not be
mistranslated. Only the step-2 enable link, the step-2 API name, the step-3
scope string and Gmail REST copy differ per mode; everything else is generic
Google Cloud Console pages (a project id does not exist until step 1, so nothing
user-specific can be deep-linked).
"""

from __future__ import annotations

import webbrowser

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from osia import i18n


# ── Per-connector data (code, not i18n) ──────────────────────────────────────
# Keyed by the catalog connector id or explicit wizard mode:
#   gmail-rest   native Gmail REST (Gmail API)
#   gmail        legacy Gmail MCP  url https://gmailmcp.googleapis.com/mcp/v1
#   google-drive Google Drive MCP   url https://drivemcp.googleapis.com/mcp/v1
_WIZARD_LINKS = {
    "gmail-rest": "https://console.developers.google.com/apis/api/gmail.googleapis.com/overview",
    "gmail": "https://console.developers.google.com/apis/api/gmailmcp.googleapis.com/overview",
    "google-drive": "https://console.developers.google.com/apis/api/drivemcp.googleapis.com/overview",
}
# Human-readable API hostname shown in the step-2 body ({api} placeholder).
_WIZARD_APIS = {
    "gmail-rest": "gmail.googleapis.com",
    "gmail": "gmailmcp.googleapis.com",
    "google-drive": "drivemcp.googleapis.com",
}
# The scope(s) the user must declare on the consent screen ({scopes}
# placeholder in the step-3 body). Mirrors the connector/integration scopes.
_WIZARD_SCOPES = {
    "gmail-rest": (
        "Required: https://www.googleapis.com/auth/gmail.readonly\n"
        "Optional for draft creation and draft sending: "
        "https://www.googleapis.com/auth/gmail.compose\n"
        "Optional for read/unread and label changes: "
        "https://www.googleapis.com/auth/gmail.modify"
    ),
    "gmail": "https://mail.google.com/",
    "google-drive": "https://www.googleapis.com/auth/drive.readonly + https://www.googleapis.com/auth/drive.file",
}
_WIZARD_ALIASES = {
    "gmail-mcp-legacy": "gmail",
}

# Generic console pages (same for every connector — see module docstring).
_LINK_PROJECT = "https://console.cloud.google.com/projectcreate"
_LINK_CONSENT = "https://console.cloud.google.com/auth/overview"
_LINK_CLIENTS = "https://console.cloud.google.com/auth/clients"


class _MCPOAuthWizardDialog(QDialog):
    """5-step guide for creating the connector's Google OAuth client.

    Constructed with ``(parent, entry, mode=None)`` where ``entry`` is the
    connector catalog dict and ``mode`` can force a specific guide such as
    ``gmail-rest``. Instructional only; closing it has no side effects.
    """

    def __init__(self, parent, entry, mode: str | None = None):
        super().__init__(parent)
        self.setObjectName("mcpOAuthWizard")
        sid = (entry or {}).get("id", "") or "gmail"
        wizard_mode = str(
            mode
            or (entry or {}).get("oauth_wizard_mode")
            or (entry or {}).get("native_integration")
            or sid
            or "gmail"
        )
        wizard_mode = _WIZARD_ALIASES.get(wizard_mode, wizard_mode)
        # Mirror the existing dialogs' flags so the modal always surfaces above the
        # FramelessWindowHint|Tool|WindowStaysOnTopHint parent popup.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)

        # Per-mode substitutions (fall back to legacy Gmail MCP for an unknown id).
        enable_url = _WIZARD_LINKS.get(wizard_mode, _WIZARD_LINKS["gmail"])
        api_name = _WIZARD_APIS.get(wizard_mode, _WIZARD_APIS["gmail"])
        scopes = _WIZARD_SCOPES.get(wizard_mode, _WIZARD_SCOPES["gmail"])
        gmail_rest = wizard_mode == "gmail-rest"
        step2_title_key = (
            "mcp.wizard.gmail_rest.step2_title"
            if gmail_rest else "mcp.wizard.step2_title"
        )
        step2_body_key = (
            "mcp.wizard.gmail_rest.step2_body"
            if gmail_rest else "mcp.wizard.step2_body"
        )
        step3_title_key = (
            "mcp.wizard.gmail_rest.step3_title"
            if gmail_rest else "mcp.wizard.step3_title"
        )
        step3_body_key = (
            "mcp.wizard.gmail_rest.step3_body"
            if gmail_rest else "mcp.wizard.step3_body"
        )
        step4_body_key = (
            "mcp.wizard.gmail_rest.step4_body"
            if gmail_rest else "mcp.wizard.step4_body"
        )
        step4_title_key = (
            "mcp.wizard.gmail_rest.step4_title"
            if gmail_rest else "mcp.wizard.step4_title"
        )
        step5_title_key = (
            "mcp.wizard.gmail_rest.step5_title"
            if gmail_rest else "mcp.wizard.step5_title"
        )
        step5_body_key = (
            "mcp.wizard.gmail_rest.step5_body"
            if gmail_rest else "mcp.wizard.step5_body"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("dialogCard")
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        title = QLabel(i18n.t("mcp.wizard.title"))
        title.setObjectName("dialogTitle")
        lay.addWidget(title)

        intro = QLabel(i18n.t("mcp.wizard.intro"))
        intro.setObjectName("sectionSub")
        intro.setWordWrap(True)
        intro.setMinimumWidth(380)
        lay.addWidget(intro)

        # ── Step pages ───────────────────────────────────────────────────────
        # (title_key, body_text, optional url). Step 5 has no link (back to OSIA).
        mobile_callback = ""
        if gmail_rest:
            try:
                from osia.integrations.google_oauth import mobile_redirect_uri

                mobile_callback = mobile_redirect_uri()
            except Exception:
                pass
        steps = [
            (
                "mcp.wizard.step1_title",
                i18n.t("mcp.wizard.step1_body"),
                _LINK_PROJECT,
            ),
            (
                step2_title_key,
                i18n.t(step2_body_key, api=api_name),
                enable_url,
            ),
            (
                step3_title_key,
                i18n.t(step3_body_key, scopes=scopes),
                _LINK_CONSENT,
            ),
            (
                step4_title_key,
                i18n.t(
                    step4_body_key,
                    callback=mobile_callback or "Pair OSIA Mobile first",
                ),
                _LINK_CLIENTS,
            ),
            (
                step5_title_key,
                i18n.t(step5_body_key),
                None,
            ),
        ]
        self._total = len(steps)

        self._stack = QStackedWidget()
        for title_key, body_text, url in steps:
            page = QWidget()
            pv = QVBoxLayout(page)
            pv.setContentsMargins(0, 0, 0, 0)
            pv.setSpacing(8)

            st = QLabel(i18n.t(title_key))
            st.setObjectName("sectionTitle")
            st.setWordWrap(True)
            pv.addWidget(st)

            sb = QLabel(body_text)
            sb.setObjectName("sectionSub")
            sb.setWordWrap(True)
            sb.setMinimumWidth(380)
            pv.addWidget(sb)

            if url:
                open_btn = QPushButton(i18n.t("mcp.wizard.open_link"))
                open_btn.setObjectName("ghostBtn")
                open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                open_btn.clicked.connect(
                    lambda _=False, u=url: self._open(u)
                )
                obrow = QHBoxLayout()
                obrow.addWidget(open_btn)
                obrow.addStretch(1)
                pv.addLayout(obrow)

            pv.addStretch(1)
            self._stack.addWidget(page)
        lay.addWidget(self._stack)

        # ── Navigation row ───────────────────────────────────────────────────
        nav = QHBoxLayout()
        nav.setSpacing(8)
        close_btn = QPushButton(i18n.t("mcp.wizard.close"))
        close_btn.setObjectName("ghostBtn")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        nav.addWidget(close_btn)

        self._indicator = QLabel("")
        self._indicator.setObjectName("sectionSub")
        nav.addWidget(self._indicator)
        nav.addStretch(1)

        self._prev_btn = QPushButton(i18n.t("mcp.wizard.prev"))
        self._prev_btn.setObjectName("ghostBtn")
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._go_prev)
        nav.addWidget(self._prev_btn)

        self._next_btn = QPushButton(i18n.t("mcp.wizard.next"))
        self._next_btn.setObjectName("primaryBtn")
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._go_next)
        nav.addWidget(self._next_btn)

        lay.addLayout(nav)

        self._update_nav()

    # ── Navigation ───────────────────────────────────────────────────────────
    def _go_prev(self) -> None:
        i = self._stack.currentIndex()
        if i > 0:
            self._stack.setCurrentIndex(i - 1)
            self._update_nav()

    def _go_next(self) -> None:
        i = self._stack.currentIndex()
        if i < self._total - 1:
            self._stack.setCurrentIndex(i + 1)
            self._update_nav()
        else:
            # Last step → the primary button reads "Done" and just closes.
            self.accept()

    def _update_nav(self) -> None:
        i = self._stack.currentIndex()
        self._indicator.setText(
            i18n.t("mcp.wizard.step_indicator", n=i + 1, total=self._total)
        )
        self._prev_btn.setEnabled(i > 0)
        self._next_btn.setText(
            i18n.t("mcp.wizard.done")
            if i == self._total - 1
            else i18n.t("mcp.wizard.next")
        )

    @staticmethod
    def _open(url: str) -> None:
        """Open a docs page in the PC browser. The desktop process has no
        ``webbrowser`` guard (only the server process neuters it), so this is the
        same mechanism the OAuth consent flow and the provider "Get key" links use.
        """
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        if parent is not None:
            try:
                geo = parent.window().frameGeometry()
                self.move(geo.center() - self.rect().center())
            except Exception:
                pass
        # Guarantee the modal surfaces above the always-on-top parent.
        self.raise_()
        self.activateWindow()

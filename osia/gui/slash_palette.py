"""Keyboard-accessible command, conversation and model pickers."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout,
)

from osia import i18n
from osia.conversations import conversation_special_mode
from osia.gui.motion import MOTION, MotionController
from osia.slash_commands import (
    MAX_RESULTS,
    connector_availability,
    conversation_availability,
    conversation_palette_items,
    current_web_available,
    palette_items,
    slash_palette_query,
)


class SlashCommandPalette(QFrame):
    item_selected = pyqtSignal(object)
    inventory_requested = pyqtSignal()
    dismissed = pyqtSignal()
    _snapshot_ready = pyqtSignal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("slashPalette")
        self.setMaximumHeight(252)
        self._locale = "en"
        self._provider_id = ""
        self._conversation_id = ""
        self._rows: list[dict] = []
        self._model_rows: list[dict] = []
        self._query: tuple[tuple[str, str, str], str] | None = None
        self._stage = ""
        self._snapshots: dict[str, tuple[tuple[str, str, str], object]] = {}
        self._loading: set[str] = set()
        self._return_focus = None
        self._motion = MotionController(self)
        self._snapshot_ready.connect(self._on_snapshot_ready)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(4)
        self._title = QLabel("Commands")
        self._title.setObjectName("slashPaletteTitle")
        layout.addWidget(self._title)
        self._filter = QLineEdit()
        self._filter.setObjectName("slashPaletteSearch")
        self._filter.installEventFilter(self)
        self._filter.textChanged.connect(self._filter_models)
        self._filter.hide()
        layout.addWidget(self._filter)
        self._list = QListWidget()
        self._list.setObjectName("slashPaletteList")
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setUniformItemSizes(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.itemClicked.connect(self._activate_item)
        layout.addWidget(self._list)
        self.hide()

    def set_context(self, *, locale: str, provider_id: str = "", conversation_id: str = "") -> None:
        self._locale = "fr" if str(locale).lower().startswith("fr") else "en"
        self.setAccessibleName("Commandes et discussions" if self._locale == "fr" else "Commands and conversations")
        self._provider_id = str(provider_id or "")
        self._conversation_id = str(conversation_id or "")

    def set_model_inventory(self, inventory: list[dict]) -> None:
        self._model_rows = [
            {
                "selection_type": "model",
                "provider_id": item["provider_id"],
                "model_id": item["model_id"],
                "name": item["model_display_name"],
                "description": item["provider_display_name"],
                "reasoning_options": item["reasoning_options"],
                "enabled": True,
            }
            for item in inventory if item["picker_visible"] and item["available"]
        ]
        if self._stage == "models":
            self._filter_models(self._filter.text())
        elif self._query and not self._stage:
            self._render_query()

    def update_from_text(
        self, text: str, *, mention_query: str | None = None, enabled: bool = True,
    ) -> None:
        query = mention_query if mention_query is not None else slash_palette_query(text)
        if not enabled or query is None:
            self.close_palette()
            return
        kind = "conversations" if mention_query is not None else "commands"
        key = (kind, self._provider_id, self._conversation_id)
        new_session = self._query is None or self._query[0] != key
        if self._stage == "models":
            self._hide_search()
        self._stage = ""
        self._query = (key, query)
        self._render_query()
        if new_session:
            self._request_snapshot(key)
            if kind == "commands":
                self.inventory_requested.emit()

    def _request_snapshot(self, key: tuple[str, str, str]) -> None:
        kind, provider_id, conversation_id = key
        if kind in self._loading:
            return
        self._loading.add(kind)

        def load() -> None:
            try:
                if kind == "conversations":
                    data = conversation_availability(current_conversation_id=conversation_id)
                else:
                    data = (
                        connector_availability(), current_web_available(provider_id),
                        conversation_special_mode(conversation_id),
                    )
            except Exception:
                data = () if kind == "conversations" else ((), False, False)
            try:
                self._snapshot_ready.emit(key, data)
            except RuntimeError:
                pass  # The popup can close while a store read is in flight.

        threading.Thread(target=load, name=f"osia-palette-{kind}", daemon=True).start()

    def _on_snapshot_ready(self, key: tuple[str, str, str], data: object) -> None:
        kind = key[0]
        self._loading.discard(kind)
        self._snapshots[kind] = (key, data)
        if self._query and not self._stage:
            active_key, _query = self._query
            if active_key == key:
                self._render_query()
            elif active_key[0] == kind:
                self._request_snapshot(active_key)

    def _render_query(self) -> None:
        key, query = self._query
        kind = key[0]
        cached = self._snapshots.get(kind)
        data = cached[1] if cached and cached[0] == key else None
        french = self._locale == "fr"
        if kind == "conversations":
            rows = conversation_palette_items(data or (), query, self._locale)
            if not rows:
                if data is None:
                    message = "Chargement des discussions…" if french else "Loading conversations…"
                else:
                    message = "Aucune discussion correspondante." if french else "No matching conversations."
                rows = [{"name": message, "enabled": False}]
            self._show_rows(rows, "@ Discussions" if french else "@ Conversations")
            return
        connectors, web_available, mouse_control_disabled = data or ((), False, False)
        self._show_rows(
            list(palette_items(
                locale=self._locale, filter_text=query,
                models_available=bool(self._model_rows),
                web_available=web_available, mouse_control_disabled=mouse_control_disabled,
                connectors=connectors,
            )),
            "Raccourcis" if french else "Shortcuts",
        )

    def _show_rows(self, rows: list[dict], title: str) -> None:
        was_hidden = self.isHidden()
        if rows != self._rows or self._title.text() != title:
            self._rows = rows
            self._title.setText(title)
            self._list.setUpdatesEnabled(False)
            try:
                self._list.clear()
                last_category = None
                for row in rows:
                    category = row.get("category")
                    if category and category != last_category:
                        label = {
                            "commands": "Commandes" if self._locale == "fr" else "Commands",
                            "connectors": "Connecteurs" if self._locale == "fr" else "Connectors",
                        }.get(category, "")
                        header = QListWidgetItem(label)
                        header.setFlags(Qt.ItemFlag.NoItemFlags)
                        self._list.addItem(header)
                        last_category = category
                    alias = row.get("canonical_alias")
                    name = row["name"]
                    description = row.get("description") or ""
                    label = f"/{alias}  {name}" if alias else name
                    item = QListWidgetItem(f"{label}  ·  {description}" if description else label)
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    item.setToolTip(description)
                    if row.get("enabled") is False:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                    self._list.addItem(item)
                self._select_first_enabled()
            finally:
                self._list.setUpdatesEnabled(True)
        self.setVisible(bool(rows))
        if rows and was_hidden:
            self._motion.fade_in(self, MOTION.state_ms)

    def _select_first_enabled(self) -> None:
        for index in range(self._list.count()):
            if self._list.item(index).flags() & Qt.ItemFlag.ItemIsEnabled:
                self._list.setCurrentRow(index)
                return

    def _hide_search(self) -> None:
        self._filter.hide()
        focus, self._return_focus = self._return_focus, None
        if focus is not None:
            try:
                focus.setFocus()
            except RuntimeError:
                pass

    def close_palette(self) -> None:
        self._query = None
        if self._stage == "models":
            self._hide_search()
        self._stage = ""
        if not self.isHidden():
            self._motion.settle()
            self.hide()
            self.dismissed.emit()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._filter and event.type() == QEvent.Type.KeyPress and self.handle_key(event):
            return True
        return super().eventFilter(watched, event)

    def handle_key(self, event) -> bool:
        if not self.isVisible():
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_palette()
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            delta = 1 if key == Qt.Key.Key_Down else -1
            count = self._list.count()
            next_row = self._list.currentRow()
            for _ in range(count):
                next_row = (next_row + delta) % count
                if self._list.item(next_row).flags() & Qt.ItemFlag.ItemIsEnabled:
                    self._list.setCurrentRow(next_row)
                    break
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return self.activate_current()
        return False

    def activate_current(self) -> bool:
        if not self.isVisible():
            return False
        item = self._list.currentItem()
        if item is not None and item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self._activate_item(item)
        return True

    def _activate_item(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, dict) or row.get("enabled") is False:
            return
        handler = row.get("handler_contract")
        if handler == "select_exact_model_v1":
            self._stage = "models"
            self._return_focus = QApplication.focusWidget()
            self._filter.setPlaceholderText(
                "Rechercher un modèle…" if self._locale == "fr" else "Search models…"
            )
            self._filter.clear()
            self._filter.show()
            self._filter_models("")
            self._filter.setFocus()
            return
        if row.get("selection_type") == "model" and "reasoning_options" in row:
            self._show_reasoning(row)
            return
        if handler == "select_skill_v1":
            self._show_skills()
            return
        self.close_palette()
        self.item_selected.emit(row)

    def _filter_models(self, text: str) -> None:
        if self._stage != "models":
            return
        needle = text.strip().casefold()
        french = self._locale == "fr"
        rows = []
        if not needle:
            rows.append({
                "selection_type": "model", "remove_override": True, "enabled": True,
                "name": "Modèle configuré" if french else "Configured model",
            })
        for row in self._model_rows:
            if not needle or any(needle in row[key].casefold() for key in ("name", "description", "model_id")):
                rows.append(row)
            if len(rows) >= MAX_RESULTS:
                break
        if not rows:
            rows.append({"name": "Aucun modèle correspondant." if french else "No matching models.",
                         "enabled": False})
        self._show_rows(rows, "Choisir un modèle" if french else "Choose a model")

    def _show_reasoning(self, model: dict) -> None:
        self._stage = "effort"
        self._hide_search()
        options = model["reasoning_options"]
        if not any(option["id"] == "auto" for option in options):
            options = [{"id": "auto"}, *options]
        rows = []
        for option in options:
            level = option["id"]
            key = "reasoning.default" if level == "auto" else f"reasoning.{level}"
            label = i18n.t(key)
            if label == key:
                label = option.get("label") or level
            rows.append({
                "selection_type": "model", "enabled": True,
                "provider_id": model["provider_id"], "model_id": model["model_id"],
                "model_name": model["name"], "reasoning_level": level,
                "reasoning_label": label, "name": label,
            })
        title = "Raisonnement" if self._locale == "fr" else "Reasoning"
        self._show_rows(rows, f"{title} · {model['name']}")

    def _show_skills(self) -> None:
        from osia.personality import discover_skills

        self._stage = "skills"
        rows = [
            {
                "selection_type": "skill", "skill_name": skill.filename,
                "name": skill.filename[:-3],
                "description": " ".join(skill.description.split())[:160],
                "enabled": skill.available,
            }
            for skill in discover_skills()
        ]
        if not rows:
            rows.append({
                "name": "Crée un skill avec /skillcreator."
                if self._locale == "fr" else "Create a skill with /skillcreator.",
                "enabled": False,
            })
        self._show_rows(rows, "Skills")

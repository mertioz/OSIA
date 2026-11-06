"""Keyboard-accessible slash-command palette for the desktop composer."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

from osia.slash_commands import (
    MAX_RESULTS,
    MAX_RESULTS_PER_CATEGORY,
    conversation_availability,
    model_availability,
    palette_items,
    slash_palette_query,
)


class SlashCommandPalette(QFrame):
    """A bounded result list. Selecting a selector opens its typed choices."""

    item_selected = pyqtSignal(object)
    dismissed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("slashPalette")
        self.setAccessibleName("Slash commands")
        self.setMaximumHeight(230)
        self._locale = "en"
        self._provider_id = ""
        self._model_id = ""
        self._conversation_id = ""
        self._rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(4)
        self._title = QLabel("Commands")
        self._title.setObjectName("slashPaletteTitle")
        layout.addWidget(self._title)
        self._list = QListWidget()
        self._list.setObjectName("slashPaletteList")
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.itemClicked.connect(self._activate_item)
        layout.addWidget(self._list)
        self.hide()

    def set_context(
        self,
        *,
        locale: str,
        provider_id: str = "",
        model_id: str = "",
        conversation_id: str = "",
    ) -> None:
        self._locale = "fr" if str(locale).lower().startswith("fr") else "en"
        self.setAccessibleName(
            "Palette de commandes slash"
            if self._locale == "fr" else "Slash command palette"
        )
        self._provider_id = str(provider_id or "")
        self._model_id = str(model_id or "")
        self._conversation_id = str(conversation_id or "")

    def update_from_text(self, text: str, *, enabled: bool = True) -> None:
        query = slash_palette_query(text) if enabled else None
        if query is None:
            self.close_palette()
            return
        rows = palette_items(
            locale=self._locale,
            filter_text=query,
            provider_id=self._provider_id,
            model_id=self._model_id,
            current_conversation_id=self._conversation_id,
        )
        self._show_rows(
            list(rows),
            "Commandes slash" if self._locale == "fr" else "Slash commands",
        )

    def _show_rows(self, rows: list[dict], title: str, *, limit: int | None = MAX_RESULTS) -> None:
        self._rows = rows[:limit]
        self._list.clear()
        self._title.setText(title)
        categorized = any("category" in row for row in self._rows)
        last_category = None
        for row in self._rows:
            alias = str(row.get("canonical_alias") or "")
            name = str(row.get("name") or "")
            description = str(row.get("description") or "")
            label = f"/{alias}  {name}" if alias else name
            category = str(row.get("category") or "commands")
            category_label = {
                "commands": "Commandes" if self._locale == "fr" else "Commands",
                "connectors": "Connecteurs" if self._locale == "fr" else "Connectors",
                "conversations": "Conversations",
            }.get(category, "")
            if categorized and category != last_category:
                header = QListWidgetItem(category_label)
                header.setFlags(Qt.ItemFlag.NoItemFlags)
                header.setData(Qt.ItemDataRole.UserRole, None)
                self._list.addItem(header)
                last_category = category
            if description:
                label += f" — {description}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row)
            if row.get("enabled") is False:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.setToolTip(description)
            self._list.addItem(item)
        self.setVisible(bool(self._rows))
        if self._rows:
            self._select_first_enabled()

    def _select_first_enabled(self) -> None:
        for index in range(self._list.count()):
            item = self._list.item(index)
            if bool(item.flags() & Qt.ItemFlag.ItemIsEnabled):
                self._list.setCurrentRow(index)
                return

    def close_palette(self) -> None:
        if self.isVisible():
            self.hide()
            self.dismissed.emit()

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
            if count:
                next_row = self._list.currentRow()
                for _ in range(count):
                    next_row = (next_row + delta) % count
                    item = self._list.item(next_row)
                    if bool(item.flags() & Qt.ItemFlag.ItemIsEnabled):
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
        if item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEnabled):
            self._activate_item(item)
        return True

    def _activate_item(self, item: QListWidgetItem) -> None:
        row = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(row, dict) or row.get("enabled") is False:
            return
        if row.get("selection_type"):
            self.item_selected.emit(row)
            self.close_palette()
            return
        handler = str(row.get("handler_contract") or "")
        if handler == "select_skill_v1":
            from osia.personality import discover_skills

            rows = [
                {
                    "selection_type": "skill",
                    "skill_name": skill.filename,
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
            self._show_rows(rows, "Skills", limit=None)
            return
        if handler == "select_exact_model_v1":
            models = [{
                "selection_type": "model",
                "remove_override": True,
                "name": "Automatique" if self._locale == "fr" else "Automatic",
                "description": "",
                "enabled": True,
            }]
            for model in model_availability(refresh=False)[
                : MAX_RESULTS_PER_CATEGORY - 1
            ]:
                provider_id = str(model.get("providerId") or "")
                model_id = str(model.get("id") or "")
                models.append({
                    "selection_type": "model",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "name": str(model.get("label") or model.get("displayName") or model_id),
                    "description": str(model.get("providerDisplayName") or provider_id),
                    "enabled": True,
                })
            self._show_selector(models, "Modèles" if self._locale == "fr" else "Models")
            return
        if handler == "select_conversation_reference_v1":
            rows = [
                {
                    "selection_type": "conversation",
                    **conversation,
                    "name": conversation["display_title"] or (
                        "Sans titre" if self._locale == "fr" else "Untitled"
                    ),
                    "description": conversation["display_project"],
                    "enabled": True,
                }
                for conversation in conversation_availability(
                    current_conversation_id=self._conversation_id
                )[:MAX_RESULTS_PER_CATEGORY]
            ]
            self._show_selector(
                rows,
                "Conversations",
            )
            return
        if handler == "select_reasoning_level_v1":
            levels = ("auto", "off", "low", "medium", "high")
            selected_model = next(
                (
                    model for model in model_availability(refresh=False)
                    if str(model.get("providerId") or "") == self._provider_id
                    and str(model.get("id") or "") == self._model_id
                ),
                None,
            )
            if selected_model is not None:
                reasoning = selected_model.get("reasoning")
                options = reasoning.get("options") if isinstance(reasoning, dict) else []
                supported = {
                    str(option.get("id") or "")
                    for option in options if isinstance(option, dict)
                }
                if supported:
                    levels = tuple(
                        level for level in levels
                        if level == "auto" or level in supported
                    )
            rows = [
                {
                    "selection_type": "reasoning",
                    "reasoning_level": level,
                    "name": level.title(),
                    "description": "",
                    "enabled": True,
                }
                for level in levels
            ]
            self._show_selector(
                rows, "Raisonnement" if self._locale == "fr" else "Reasoning"
            )
            return
        self.item_selected.emit(row)
        self.close_palette()

    def _show_selector(self, rows: list[dict], title: str) -> None:
        normalized = []
        for row in rows[:MAX_RESULTS_PER_CATEGORY]:
            item = dict(row)
            item["canonical_alias"] = ""
            normalized.append(item)
        self._show_rows(normalized, title)

"""Canonical, sanitized permission metadata shared with OSIA Mobile."""

from __future__ import annotations

from dataclasses import dataclass

from osia.action_policy import AUTOMATIC_PERMISSION_CATEGORIES


MOBILE_PERMISSION_CONTRACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PermissionCategoryContract:
    identifier: str
    label_key: str
    description_key: str
    delegation_rule: str = "ordinary_actions_only"
    mobile_request_allowed: bool = True

    def mobile_projection(self) -> dict[str, object]:
        return {
            "id": self.identifier,
            "label_key": self.label_key,
            "description_key": self.description_key,
            "delegation_rule": self.delegation_rule,
            "mobile_request_allowed": self.mobile_request_allowed,
        }


PERMISSION_CATEGORY_CONTRACT = (
    PermissionCategoryContract(
        "files_modify",
        "permissions.category.files_modify",
        "permissions.switch.files_modify",
    ),
    PermissionCategoryContract(
        "files_delete",
        "permissions.category.files_delete",
        "permissions.switch.files_delete",
    ),
    PermissionCategoryContract(
        "commands",
        "permissions.category.commands",
        "permissions.switch.commands",
    ),
    PermissionCategoryContract(
        "browser_account",
        "permissions.category.browser_account",
        "permissions.switch.browser_account",
    ),
    PermissionCategoryContract(
        "connectors",
        "permissions.category.connectors",
        "permissions.switch.connectors",
    ),
    PermissionCategoryContract(
        "send_publish_submit",
        "permissions.category.send_publish_submit",
        "permissions.switch.send_publish_submit",
    ),
    PermissionCategoryContract(
        "purchases_payments",
        "permissions.category.purchases_payments",
        "permissions.switch.purchases_payments",
        delegation_rule="ordinary_actions_with_pc_limits",
    ),
    PermissionCategoryContract(
        "autonomous_actions",
        "permissions.category.autonomous_actions",
        "permissions.switch.autonomous_actions",
        delegation_rule="explicit_scoped_or_ordinary_actions",
    ),
    PermissionCategoryContract(
        "scheduled_actions",
        "permissions.category.scheduled_actions",
        "permissions.switch.scheduled_actions",
        delegation_rule="explicit_scoped_or_ordinary_actions",
    ),
)


if tuple(item.identifier for item in PERMISSION_CATEGORY_CONTRACT) != tuple(
    AUTOMATIC_PERMISSION_CATEGORIES
):
    raise RuntimeError("permission_category_contract_out_of_sync")


def mobile_permission_contract() -> dict[str, object]:
    """Return the value-free PC contract consumed by the Mobile UI."""

    return {
        "schema_version": MOBILE_PERMISSION_CONTRACT_SCHEMA_VERSION,
        "custom_access": "derived",
        "full_access_activation": "local_pc_ceremony",
        "mobile_changes": "canonical_requests",
        "categories": [
            item.mobile_projection() for item in PERMISSION_CATEGORY_CONTRACT
        ],
    }


__all__ = [
    "MOBILE_PERMISSION_CONTRACT_SCHEMA_VERSION",
    "PERMISSION_CATEGORY_CONTRACT",
    "PermissionCategoryContract",
    "mobile_permission_contract",
]

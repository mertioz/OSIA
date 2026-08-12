"""Small backend contract for OSIA's semantic browser capability.

The isolated Playwright implementation remains in :mod:`osia.tools.browser`.
Backends exposed here only select and transport the existing semantic
operations; they do not redefine browser state or receipt contracts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class BrowserBackend(ABC):
    """Minimal lifecycle shared by isolated and explicitly personal browsers."""

    name: str

    @abstractmethod
    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def revoke(self) -> dict[str, Any]:
        raise NotImplementedError


__all__ = ["BrowserBackend"]

"""Small contract for OSIA's isolated semantic browser adapter.

The Playwright implementation remains in :mod:`osia.tools.browser`; this
adapter forwards operations without redefining browser state or receipts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping


class BrowserBackend(ABC):
    """Minimal lifecycle for the supported OSIA browser runtime."""

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

"""Local-only Personal Browser bridge.

No module in this package opens a TCP, HTTP, WebSocket, Worker, or relay
transport.  Chromium Native Messaging reaches OSIA only through same-user IPC.
"""

from osia.personal_browser.protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]

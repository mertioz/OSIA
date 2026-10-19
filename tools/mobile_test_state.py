"""Opt-in scratch stores for authenticated Mobile server contract fixtures."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
from unittest.mock import patch


@contextmanager
def isolated_mobile_server_state(root: Path):
    from osia import config, conversations, durable_transport, turn_terminality, document_sources
    from osia.server import server

    root = Path(root)
    settings = root / "config"
    settings.mkdir(parents=True, exist_ok=True)
    store = durable_transport.DurableTransportStore(root / "transport")
    with ExitStack() as stack:
        for module, name, value in [
            (config, "CONFIG_DIR", str(settings)),
            (config, "PRIVATE_DATA_DIR", str(root / "private")),
            (config, "SETTINGS_FILE", str(settings / "settings.json")),
            (config, "API_KEYS_FILE", str(settings / "api_keys.json")),
            (config, "MOBILE_TOKEN_FILE", str(settings / "mobile_token.txt")),
            (conversations, "CONVERSATIONS_DIR", root / "conversations"),
            (turn_terminality, "CONFIG_DIR", str(settings)),
            (durable_transport, "_DEFAULT_STORE", store),
            (server, "_DURABLE_TRANSPORT", store),
            (server, "_TURN_EVENT_SEQUENCE_DIR", root / "sequences"),
            (document_sources, "_STORE", document_sources.DocumentSourceStore(root / "sources")),
        ]:
            stack.enter_context(patch.object(module, name, value))
        yield

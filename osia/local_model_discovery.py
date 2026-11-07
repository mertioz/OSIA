"""Read local model inventories without launching or modifying a runtime."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests


def _json_list(session, url: str, field: str, *, headers=None) -> list | None:
    try:
        timeout = (0.4 if _is_loopback(url) else 1.0, 1.5)
        response = session.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (requests.ConnectionError, requests.Timeout):
        raise
    except (requests.RequestException, ValueError):
        return None
    value = payload.get(field) if isinstance(payload, dict) else None
    return value if isinstance(value, list) else None


def _is_loopback(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
        if host.casefold() == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _server_root(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/v1")


def ollama_loopback_endpoint(default: str) -> str:
    """Use Ollama's configured loopback port for both discovery and inference."""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return default
    if "://" not in host:
        host = "http://" + host
    try:
        parsed = urlsplit(host)
        port = parsed.port or 11434
    except ValueError:
        return default
    if (
        parsed.scheme not in {"http", "https"} or parsed.username is not None
        or parsed.query or parsed.fragment or parsed.path.strip("/")
    ):
        return default
    if parsed.hostname in {"0.0.0.0", "::"}:
        netloc = "127.0.0.1" if parsed.hostname == "0.0.0.0" else "[::1]"
    elif _is_loopback(host):
        netloc = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    else:
        return default
    return f"{parsed.scheme}://{netloc}:{port}/v1"


def _identifier(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        return ""
    if any(ord(char) < 32 for char in value):
        return ""
    return value.strip()


def _model(provider: str, identity: str, *, available: bool, label: str = "", **metadata) -> dict:
    return {
        "id": identity, "label": label or identity, "provider": provider, "tier": "light",
        "available": available, "availability": "listed" if available else "installed",
        **metadata,
    }


def _read_local_json(path: Path, limit: int):
    try:
        with path.open(encoding="utf-8") as stream:
            text = stream.read(limit + 1)
        return json.loads(text) if len(text) <= limit else None
    except (OSError, ValueError):
        return None


def _ollama_disk_models() -> list[dict]:
    configured = os.environ.get("OLLAMA_MODELS", "").strip()
    directory = Path(configured).expanduser() if configured else Path.home() / ".ollama" / "models"
    manifests = directory / "manifests"
    models = []
    try:
        for entry in manifests.glob("*/*/*/*"):
            if not entry.is_file():
                continue
            host, namespace, name, tag = entry.relative_to(manifests).parts
            if any(part.startswith(".") or any(char.isspace() for char in part)
                   for part in (host, namespace, name, tag)):
                continue
            manifest = _read_local_json(entry, 2 * 1024 * 1024)
            if not isinstance(manifest, dict) or not any(
                manifest.get(field) for field in ("layers", "manifests", "digest")
            ):
                continue
            prefix = f"{host}/{namespace}/" if host != "registry.ollama.ai" else (
                "" if namespace == "library" else f"{namespace}/"
            )
            models.append(_model("ollama", f"{prefix}{name}:{tag}", available=False))
    except OSError:
        pass
    return models


def query_ollama_models(base_url: str) -> list[dict]:
    root = _server_root(base_url)
    with requests.Session() as session:
        # Never send discovery requests for loopback runtimes through a proxy.
        session.trust_env = not _is_loopback(root)
        try:
            rows = _json_list(session, root + "/api/tags", "models")
            if rows is None:
                # Also supports a configured local OpenAI-compatible endpoint.
                rows = _json_list(session, base_url.rstrip("/") + "/models", "data")
        except (requests.ConnectionError, requests.Timeout):
            rows = None
    if rows is None:
        return _ollama_disk_models() if _is_loopback(root) else []
    identities = dict.fromkeys(
        _identifier(row.get("model") or row.get("name") or row.get("id"))
        for row in rows if isinstance(row, dict) and not (row.get("remote_host") or row.get("remote_model"))
    )
    return [_model("ollama", mid, available=True) for mid in identities if mid]


def _lmstudio_disk_models() -> tuple[dict[str, dict], dict[str, str]]:
    """Project verified files from LM Studio's optional on-disk model index."""
    home = Path(os.environ.get("LMSTUDIO_HOME") or (Path.home() / ".lmstudio"))
    path = home / ".internal" / "model-index-cache.json"
    data = _read_local_json(path, 16 * 1024 * 1024)
    rows = data.get("models") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}, {}
    models, aliases = {}, {}
    for row in rows:
        if not isinstance(row, dict) or row.get("domain") != "llm":
            continue
        mid = _identifier(row.get("indexedModelIdentifier"))
        entry = row.get("entryPoint")
        filename = entry.get("absPath") if isinstance(entry, dict) else None
        if not filename and row.get("format") == "mlx":
            filename = row.get("concreteModelDirAbsolutePath")
        if not mid or not isinstance(filename, str) or not Path(filename).is_absolute():
            continue
        try:
            if not Path(filename).exists():
                continue
        except OSError:
            continue
        models[mid] = _model("lmstudio", mid, available=False,
                              label=_identifier(row.get("displayName")))
        alias = _identifier(row.get("defaultIdentifier"))
        if alias:
            aliases[alias] = mid if alias not in aliases else ""
    return models, aliases


def _lmstudio_metadata(row: dict, instance=None) -> dict:
    caps = row.get("capabilities")
    result = {}
    if isinstance(caps, dict):
        if isinstance(caps.get("vision"), bool):
            result["vision"] = caps["vision"]
        if isinstance(caps.get("trained_for_tool_use"), bool):
            result["tool_calls"] = caps["trained_for_tool_use"]
    config = instance.get("config") if isinstance(instance, dict) else None
    context = config.get("context_length") if isinstance(config, dict) else None
    context = context or row.get("max_context_length")
    if type(context) is int and context > 0:
        result["context_window"] = context
    return result


def query_lmstudio_models(base_url: str, headers=None) -> list[dict]:
    root = _server_root(base_url)
    native = compatible = None
    with requests.Session() as session:
        session.trust_env = not _is_loopback(root)
        reachable = True
        try:
            native = _json_list(session, root + "/api/v1/models", "models", headers=headers)
        except requests.ConnectionError:
            reachable = False
        except requests.Timeout:
            pass
        if reachable:
            try:
                compatible = _json_list(session, base_url.rstrip("/") + "/models", "data", headers=headers)
            except (requests.ConnectionError, requests.Timeout):
                pass

    inventory, disk_aliases = _lmstudio_disk_models() if native is None and _is_loopback(root) else ({}, {})
    instance_metadata, excluded = {}, set()
    for row in native or []:
        if not isinstance(row, dict):
            continue
        mid = _identifier(row.get("key"))
        instances = row.get("loaded_instances")
        instances = [entry for entry in instances if isinstance(entry, dict)] if isinstance(instances, list) else []
        if row.get("type") != "llm":
            excluded.update([mid, *(_identifier(entry.get("id")) for entry in instances)])
            continue
        label = _identifier(row.get("display_name")) or mid
        if mid:
            inventory[mid] = _model("lmstudio", mid, available=False, label=label, **_lmstudio_metadata(row))
        for instance in instances:
            alias = _identifier(instance.get("id"))
            if alias:
                instance_metadata[alias] = (label, _lmstudio_metadata(row, instance))

    for row in compatible or []:
        if not isinstance(row, dict):
            continue
        mid = _identifier(row.get("id"))
        if not mid or mid in excluded or row.get("type") == "embedding" or mid.startswith("text-embedding-"):
            continue
        label, metadata = instance_metadata.get(mid, ("", {}))
        previous = inventory.get(mid)
        if previous is None and disk_aliases.get(mid):
            previous = inventory.pop(disk_aliases[mid], None)
        if previous is not None and not label:
            inventory[mid] = {**previous, "id": mid, "available": True, "availability": "listed"}
        else:
            inventory[mid] = _model("lmstudio", mid, available=True, label=label, **metadata)
    return list(inventory.values())

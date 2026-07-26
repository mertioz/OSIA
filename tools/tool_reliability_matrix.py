#!/usr/bin/env python3
"""Generate the WOR-178 tool evidence inventory without executing tools.

Reports are written only to the explicit caller-selected directory. They
contain contract metadata and availability categories, never arguments,
results, connector payloads, user paths or credentials.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _configured_connectors() -> list[dict[str, str]]:
    try:
        from osia.config import get_setting

        configured = get_setting("mcp_servers", []) or []
    except Exception:
        return []
    inventory = []
    for item in configured:
        if not isinstance(item, dict):
            continue
        connector_id = str(item.get("id") or "")
        if connector_id:
            inventory.append({"id": connector_id[:128]})
    return inventory


def _markdown(rows: list[dict], totals: Mapping[str, int]) -> str:
    lines = [
        "# OSIA tool reliability matrix (WOR-178)",
        "",
        "Generated from authoritative contract metadata. No tools were executed.",
        "",
        f"Static/connected tool rows: {sum(1 for row in rows if not row['tool_name'].startswith('connector::'))}",
        f"Configured unavailable connector rows: {sum(1 for row in rows if row['tool_name'].startswith('connector::'))}",
        "",
        "## Evidence totals",
        "",
    ]
    for category, count in sorted(totals.items()):
        lines.append(f"- `{category}`: {count}")
    lines.extend([
        "",
        "## Matrix",
        "",
        "| Tool | Side effect | Confirmation | Retry | Executable | Evidence | Real validation |",
        "|---|---|---|---|---:|---|---|",
    ])
    for row in rows:
        cells = [
            row["tool_name"],
            row["side_effect_class"],
            row["confirmation_policy"],
            row["retry_policy"],
            "yes" if row["currently_executable"] else "no",
            row["current_evidence_classification"],
            row["real_validation_requirement"],
        ]
        safe_cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in cells]
        lines.append("| " + " | ".join(safe_cells) + " |")
    return "\n".join(lines) + "\n"


def _runtime_inventory(rows: list[dict]) -> dict:
    dependencies = {}
    for module_name in ("jsonschema", "playwright", "psutil", "pyperclip", "PIL"):
        dependencies[module_name] = (
            "available"
            if importlib.util.find_spec(module_name) is not None
            else "unavailable_in_environment"
        )
    return {
        "platform": sys.platform,
        "static_tool_count": sum(
            1 for row in rows
            if not row["tool_name"].startswith(("mcp__", "connector::"))
        ),
        "connected_mcp_tool_count": sum(
            1 for row in rows if row["tool_name"].startswith("mcp__")
        ),
        "configured_unavailable_connector_count": sum(
            1 for row in rows if row["tool_name"].startswith("connector::")
        ),
        "dependency_status": dependencies,
    }


def build_reports(
    output_dir: Path,
    *,
    configured_connectors: Iterable[Mapping[str, object]] = (),
) -> tuple[Path, Path, list[dict]]:
    from osia.tool_contracts import matrix_rows

    rows = matrix_rows(configured_connectors=configured_connectors)
    totals = Counter(row["current_evidence_classification"] for row in rows)
    payload = {
        "schema_version": 1,
        "generator": "WOR-178 authoritative tool registry",
        "tool_execution_performed": False,
        "runtime_inventory": _runtime_inventory(rows),
        "evidence_totals": dict(sorted(totals.items())),
        "rows": rows,
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "tool-reliability-matrix.json"
    markdown_path = output_dir / "tool-reliability-matrix.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(rows, totals), encoding="utf-8")
    return json_path, markdown_path, rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Caller-selected directory for JSON and Markdown reports",
    )
    args = parser.parse_args(argv)
    try:
        json_path, markdown_path, rows = build_reports(
            args.output_dir,
            configured_connectors=_configured_connectors(),
        )
    except Exception as exc:
        print(f"matrix generation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not rows or any(row.get("schema_status") == "failed" for row in rows):
        print("matrix generation found unexpected registry failures", file=sys.stderr)
        return 2
    print(f"generated {len(rows)} metadata rows")
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

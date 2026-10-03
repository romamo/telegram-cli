"""Plain-text rendering for terminals: lists as aligned tables, objects as key: value lines."""
from __future__ import annotations

import json
from typing import Any

_MAX_CELL = 60


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1] + "…"


def _table(rows: list[dict[str, Any]]) -> str:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    cells = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [max(len(col), *(len(r[i]) for r in cells)) for i, col in enumerate(columns)]
    lines = ["  ".join(col.ljust(w) for col, w in zip(columns, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in cells]
    return "\n".join(line.rstrip() for line in lines)


def render_plain(data: Any) -> str:
    """The app's `--format plain` renderer"""
    if isinstance(data, list):
        if not data:
            return "(no results)"
        if all(isinstance(row, dict) for row in data):
            return _table(data)
    if isinstance(data, dict):
        return "\n".join(f"{key}: {_cell(value)}" for key, value in data.items())
    return json.dumps(data, ensure_ascii=False, indent=2)

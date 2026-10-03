"""Plain-text rendering for terminals: lists as aligned tables, objects as key: value lines."""
from __future__ import annotations

import json
from typing import Any

_MAX_CELL = 60


def _scalar(value: Any) -> str:
    """One value on one line: newlines become spaces so a line stays one item"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("\n", " ").replace("\r", " ")


def _cell(value: Any) -> str:
    """A table cell: one line, truncated so columns stay readable"""
    text = _scalar(value)
    return text if len(text) <= _MAX_CELL else text[: _MAX_CELL - 1] + "…"


def _pairs(value: Any, path: str) -> list[tuple[str, str]]:
    """Nested values flatten to dotted paths (`a.b`, `items.0.id`), like treaty's plain;
    an empty container keeps its key and shows `{}` or `[]`"""
    if isinstance(value, dict):
        if not value:
            return [(path, "{}")]
        return [pair for key, item in value.items() for pair in _pairs(item, f"{path}.{key}")]
    if isinstance(value, list):
        if not value:
            return [(path, "[]")]
        return [pair for i, item in enumerate(value) for pair in _pairs(item, f"{path}.{i}")]
    return [(path, _scalar(value))]


def _key_values(data: dict[str, Any]) -> str:
    pairs = [pair for key, value in data.items() for pair in _pairs(value, str(key))]
    return "".join(f"{key}: {text}".rstrip() + "\n" for key, text in pairs)


def _table(rows: list[dict[str, Any]]) -> str:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    cells = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [max(len(col), *(len(r[i]) for r in cells)) for i, col in enumerate(columns)]
    lines = ["  ".join(col.ljust(w) for col, w in zip(columns, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in cells]
    return "".join(line.rstrip() + "\n" for line in lines)


def render_plain(data: Any) -> str:
    """The app's `--format plain` renderer; like treaty's, every line ends with a newline.
    Lists of objects are tables (cells truncated); an object is untruncated key: value lines"""
    if isinstance(data, list):
        if not data:
            return "(no results)\n"
        if all(isinstance(row, dict) for row in data):
            return _table(data)
    if isinstance(data, dict):
        return _key_values(data)
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

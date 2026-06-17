from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any


def expand_env(value: Any) -> Any:
    """Recursively expands ${ENV_NAME} placeholders."""
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    return pattern.sub(lambda m: os.getenv(m.group(1), ""), value)


def get_path(data: Any, dotted_path: str, default: Any = None) -> Any:
    if not dotted_path:
        return data
    cur = data
    for part in dotted_path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, default)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return default
            cur = cur[idx]
        else:
            return default
        if cur is None:
            return default
    return cur


def to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    if cleaned in {"", "-", "."}:
        return default
    try:
        return int(float(cleaned))
    except ValueError:
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return default


def to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = re.sub(r"[^0-9]", "", str(value))
    for fmt in ("%Y%m%d", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def ensure_parent(path: str | Path) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

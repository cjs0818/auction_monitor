from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .ui_config import ensure_config_defaults
from .utils import expand_env


def load_config(
    path: str | Path = "config/config.yaml",
    *,
    expand_environment: bool = True,
) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        example = path.with_name("config.example.yaml")
        if example.exists():
            path = example
        else:
            raise FileNotFoundError(f"설정 파일이 없습니다: {path}")
    with path.open("r", encoding="utf-8") as f:
        cfg = ensure_config_defaults(yaml.safe_load(f) or {})
    return expand_env(cfg) if expand_environment else cfg


def save_config(config: dict[str, Any], path: str | Path = "config/config.yaml") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = ensure_config_defaults(config)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(normalized, f, allow_unicode=True, sort_keys=False)

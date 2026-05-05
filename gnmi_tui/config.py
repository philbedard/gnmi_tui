from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class AppConfig:
    target: str
    username: str
    password: str
    paths: list[str]
    insecure: bool = True
    skip_verify: bool = True
    encoding: str = "json_ietf"
    subscription_mode: str = "sample"
    sample_interval: int = 30_000_000_000
    history_limit: int = 300
    debug: bool = False
    log_file: str = "gnmi_tui_debug.log"


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Config file root must be a mapping/object")
    return data

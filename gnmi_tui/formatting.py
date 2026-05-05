from __future__ import annotations

import json
from typing import Any


def format_value(value: Any, max_len: int | None = 120) -> str:
    if value is None:
        return "null"

    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    else:
        rendered = str(value)

    if max_len is not None and len(rendered) > max_len:
        return f"{rendered[:max_len - 3]}..."
    return rendered


def render_detail(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    return str(value)

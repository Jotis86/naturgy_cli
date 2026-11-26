"""JSON rendering. stdout carries only the result; stderr carries errors and prompts."""

from __future__ import annotations

import json
from typing import Any

from .errors import NaturgyError


def render(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def render_error(error: NaturgyError) -> str:
    return json.dumps(error.to_payload(), ensure_ascii=False, separators=(",", ":"))

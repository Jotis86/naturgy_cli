"""Keep the Claude Code skill in sync with the CLI it documents.

`skills/naturgy/SKILL.md` tells an agent which commands exist; if a command is renamed or removed,
the skill would send the agent at something that no longer works. These tests fail in that case.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer

from naturgy.cli import app

SKILL = Path(__file__).resolve().parents[1] / "skills" / "naturgy" / "SKILL.md"

# Commands the skill must never invoke on its own, so it may leave them undocumented.
AGENT_FORBIDDEN = {"auth login", "auth logout", "auth import"}

INVOCATION = re.compile(r"\bnaturgy ((?:[a-z][a-z0-9-]*)(?: [a-z][a-z0-9-]*)*)")


def _command_paths(typer_app: typer.Typer, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for command in typer_app.registered_commands:
        if command.name:
            name = command.name
        elif command.callback is not None:
            name = command.callback.__name__.replace("_", "-")
        else:  # pragma: no cover - typer always sets one of the two
            continue
        paths.add(f"{prefix}{name}".strip())
    for group in typer_app.registered_groups:
        assert group.typer_instance is not None
        paths.add(f"{prefix}{group.name}".strip())
        paths |= _command_paths(group.typer_instance, f"{prefix}{group.name} ")
    return paths


def _group_paths(typer_app: typer.Typer, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for group in typer_app.registered_groups:
        assert group.typer_instance is not None
        paths.add(f"{prefix}{group.name}".strip())
        paths |= _group_paths(group.typer_instance, f"{prefix}{group.name} ")
    return paths


def _documented_invocations(text: str) -> set[str]:
    """Longest known command path of every `naturgy ...` mention in the skill."""
    known = _command_paths(app)
    documented: set[str] = set()
    for match in INVOCATION.finditer(text):
        tokens = match.group(1).split()
        for size in range(len(tokens), 0, -1):
            candidate = " ".join(tokens[:size])
            if candidate in known:
                documented.add(candidate)
                break
    return documented


def test_skill_mentions_only_real_commands():
    text = SKILL.read_text(encoding="utf-8")
    known = _command_paths(app)

    for match in INVOCATION.finditer(text):
        tokens = match.group(1).split()
        candidate = " ".join(tokens)
        prefix = " ".join(tokens[:2]) if len(tokens) > 1 else candidate
        assert any(
            path == candidate or candidate.startswith(f"{path} ") or path == prefix
            for path in known
        ), f"skill mentions `naturgy {candidate}`, which is not a command"


def test_skill_documents_every_command_an_agent_may_use():
    documented = _documented_invocations(SKILL.read_text(encoding="utf-8"))

    required = _command_paths(app) - _group_paths(app) - AGENT_FORBIDDEN
    missing = required - documented
    assert not missing, f"skill does not document: {sorted(missing)}"


def test_skill_forbids_claims_and_raw_output():
    text = " ".join(SKILL.read_text(encoding="utf-8").split())

    assert "naturgy invoices claim" in text
    assert "Never run `naturgy invoices claim`" in text
    assert "Never pass `--raw`" in text
    assert "naturgy auth login" in text
    assert "must be typed by the user" in text


def test_skill_has_frontmatter():
    lines = SKILL.read_text(encoding="utf-8").splitlines()

    assert lines[0] == "---"
    closing = lines.index("---", 1)
    frontmatter = "\n".join(lines[1:closing])
    assert "name: naturgy" in frontmatter
    assert "description:" in frontmatter

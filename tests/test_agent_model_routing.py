from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / ".claude/agents"
# Root AGENTS.md carries the routing policy: the former CLAUDE.md was merged
# into it and deleted, so scanning AGENTS.md covers the whole root contract.
ROUTING_FILES = (
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "README.md",
    *sorted((REPO_ROOT / ".agents").rglob("*.md")),
    *sorted((REPO_ROOT / ".codex").glob("*.md")),
    *sorted((REPO_ROOT / ".claude/rules").glob("*.md")),
    *sorted((REPO_ROOT / ".claude/hooks").glob("*.py")),
)
BARE_GENERAL_PURPOSE = re.compile(
    r"(?<![A-Za-z0-9-])general-purpose(?!-(?:deep|standard)|[A-Za-z0-9-])"
)
# Agent-Teams teammates each skill spawns, and the role each must be given. A
# teammate spawned without an agent type runs on the default subagent model
# with none of the role's tools or instructions, whatever the prose says.
TEAMMATE_ROLES = {
    "feature": {
        "Researcher": "general-purpose-deep",
        "Architect": "general-purpose-deep",
    },
    "spike": {
        "Researcher": "general-purpose-deep",
        "Feasibility Analyst": "general-purpose-deep",
    },
    "troubleshoot": {
        "Root Cause Analyst": "general-purpose-deep",
        "Impact Investigator": "general-purpose-deep",
    },
    "team-execute": {
        "Implementer-{module}": "general-purpose-standard",
        "Tester": "general-purpose-standard",
    },
}


def read_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"

    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            return frontmatter
        key, separator, value = line.partition(":")
        if separator:
            frontmatter[key.strip()] = value.strip().strip('"')

    raise AssertionError(f"Unclosed frontmatter: {path}")


def frontmatter_lines(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    return lines[1 : lines.index("---", 1)]


def test_general_purpose_agents_are_defined() -> None:
    for name in ("general-purpose-deep", "general-purpose-standard"):
        assert read_frontmatter(AGENTS_DIR / f"{name}.md")["name"] == name

    assert not (AGENTS_DIR / "general-purpose.md").exists()


def test_every_agent_runs_the_model_the_manifest_assigns(
    resolved_models: dict[str, Any],
) -> None:
    """The manifest is the only place an agent's model is chosen.

    The agent files and the manifest keys must be the same set, so an agent
    cannot be added without a reviewed model, and each file carries exactly one
    ``model:`` line holding the assigned value.
    """
    assigned = resolved_models["claude"]["agents"]
    agent_files = sorted(AGENTS_DIR.glob("*.md"))

    assert {path.stem for path in agent_files} == set(assigned)
    assert agent_files, "no agent definitions found"
    for path in agent_files:
        frontmatter = read_frontmatter(path)
        assert frontmatter["name"] == path.stem
        assert frontmatter["model"] == assigned[path.stem], path.name
        model_lines = [
            line for line in frontmatter_lines(path) if line.startswith("model:")
        ]
        assert len(model_lines) == 1, path.name


def test_settings_models_match_the_manifest(resolved_models: dict[str, Any]) -> None:
    settings = json.loads(
        (REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8")
    )

    assert (
        settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        == resolved_models["claude"]["subagent_default"]
    )
    assert settings["model"] == resolved_models["claude"]["main"]
    # A settings-file env value is exported to every child process, where it
    # would shadow the manifest for each Codex wrapper call.
    assert "CODEX_MODEL" not in settings["env"]


def test_routing_docs_do_not_reference_removed_general_purpose_agent() -> None:
    stale_references = []
    for path in ROUTING_FILES:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if BARE_GENERAL_PURPOSE.search(line):
                stale_references.append(f"{path.relative_to(REPO_ROOT)}:{line_number}")

    assert stale_references == []


def test_team_execution_defaults_to_the_standard_role_with_deep_escalation() -> None:
    team_execute = (REPO_ROOT / ".claude/skills/team-execute/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "general-purpose-standard" in team_execute
    assert "general-purpose-deep" in team_execute


@pytest.mark.parametrize("skill", sorted(TEAMMATE_ROLES))
def test_spawn_templates_name_the_role_of_every_teammate(skill: str) -> None:
    """The line under each teammate's heading names its agent type first."""
    lines = (
        (REPO_ROOT / ".claude/skills" / skill / "SKILL.md")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    defined = {path.stem for path in AGENTS_DIR.glob("*.md")}

    for teammate, role in TEAMMATE_ROLES[skill].items():
        headings = [
            index
            for index, line in enumerate(lines)
            if re.match(rf"\d+\. \*\*{re.escape(teammate)}\*\*", line)
        ]
        assert len(headings) == 1, f"{skill}: {teammate} is spawned {len(headings)}x"
        agent_line = lines[headings[0] + 1].strip()
        assert agent_line.startswith(f"Agent: `{role}`"), f"{skill}: {teammate}"
        assert role in defined

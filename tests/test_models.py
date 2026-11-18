"""Behavioral tests for the model manifest tool (``_shared/models.py``).

The tool is the only thing that writes model values into files a project owns,
so the cases below pin the properties that make that safe: a deliberate project
value is never overwritten, an unusual layout is refused instead of guessed at,
one refusal means nothing is written anywhere, and a second run changes
nothing. Every case runs against a fixture tree under ``tmp_path`` with its own
manifest, so no assertion depends on the models this repository currently uses.

The CLI cases run under every interpreter in ``INTERPRETERS``: the tool is
invoked by ``scripts/update.sh`` in downstream projects, where ``python3`` may
be the macOS system Python 3.9.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".claude" / "skills" / "_shared" / "models.py"
MANIFEST_REL = ".claude/skills/_shared/models.json"
LOCAL_REL = ".claude/models.local.json"
SETTINGS_REL = ".claude/settings.json"
CONFIG_REL = ".codex/config.toml"
OLDEST_SUPPORTED = (3, 9)
FIRST_WITH_TOMLLIB = (3, 11)
# The tool's exit codes. "Refused, nothing written" and "a write failed" are
# different codes: the shell callers continue after the first and fail on the
# second, and they tell them apart by nothing else.
EXIT_REFUSED = 2
EXIT_FAILED = 3
SYSTEM_PYTHON = Path("/usr/bin/python3")


def _version_of(interpreter: Path) -> tuple[int, int] | None:
    if not interpreter.exists():
        return None
    result = subprocess.run(
        [str(interpreter), "-c", "import sys; print(*sys.version_info[:2])"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    major, minor = result.stdout.split()
    return int(major), int(minor)


def _interpreters() -> list[str]:
    """The test interpreter, plus the system one when it is a different version."""
    found = [sys.executable]
    version = _version_of(SYSTEM_PYTHON)
    if version and version >= OLDEST_SUPPORTED and version != sys.version_info[:2]:
        found.append(str(SYSTEM_PYTHON))
    return found


INTERPRETERS = _interpreters()

FIXTURE_MANIFEST: dict[str, Any] = {
    "schema_version": 1,
    "claude": {
        "main": "fable",
        "subagent_default": "opus",
        "agents": {"alpha": "opus", "beta": "fable"},
    },
    "codex": {"model": "gpt-9.1-test", "reasoning_effort": "high"},
    "superseded": {
        "claude.main": ["haiku"],
        "claude.subagent_default": ["sonnet"],
        "codex.model": ["gpt-8-old"],
        "codex.reasoning_effort": ["medium"],
    },
}

FIXTURE_SETTINGS: dict[str, Any] = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "language": "japanese",
    "statusMessage": "━ 作業中 ━",
    "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command"}]}]},
    "env": {"EDITOR": "code --wait", "CLAUDE_CODE_SUBAGENT_MODEL": "opus"},
    "model": "fable",
}

FIXTURE_CONFIG = """\
# Codex config
model = "gpt-9.1-test"
model_reasoning_effort = "high"
model_reasoning_summary = "detailed"

approval_policy = "never"

# Feature toggles
[features]
skills = true

[profiles.fast]
model = "gpt-profile-model"
"""


def agent_text(name: str, model: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "The {name} agent"\n'
        "tools: Read, Grep\n"
        f"model: {model}\n"
        "---\n"
        "\n"
        "Body text. The next line is prose, not frontmatter:\n"
        "model: sonnet\n"
    )


def canonical(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so a fixture's line endings reach the disk untranslated.
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def read(root: Path, rel: str) -> str:
    with (root / rel).open(encoding="utf-8", newline="") as handle:
        return handle.read()


def make_project(root: Path, manifest: dict[str, Any] | None = None) -> Path:
    """Build a project whose every managed location matches the manifest."""
    write(root, MANIFEST_REL, canonical(manifest or FIXTURE_MANIFEST))
    write(root, ".claude/agents/alpha.md", agent_text("alpha", "opus"))
    write(root, ".claude/agents/beta.md", agent_text("beta", "fable"))
    write(root, SETTINGS_REL, canonical(FIXTURE_SETTINGS))
    write(root, CONFIG_REL, FIXTURE_CONFIG)
    return root


def settings_with(**env: Any) -> dict[str, Any]:
    """The fixture settings with some ``env`` entries replaced."""
    settings = copy.deepcopy(FIXTURE_SETTINGS)
    settings["env"].update(env)
    return settings


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under *root*: proves "nothing written" and "no debris"."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


Runner = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(params=INTERPRETERS, ids=lambda path: Path(path).name)
def run(request: pytest.FixtureRequest) -> Runner:
    """Invoke the CLI under one of the supported interpreters."""

    def _run(
        root: Path, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [request.param, str(SCRIPT), "--project-root", str(root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    return _run


def payload_of(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


# --- the manifest schema ------------------------------------------------------


def _manifest_with(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    manifest = copy.deepcopy(FIXTURE_MANIFEST)
    holder = manifest
    for key in path[:-1]:
        holder = holder[key]
    holder[path[-1]] = value
    return manifest


INVALID_MANIFESTS = {
    "unknown top-level key": _manifest_with(("models",), {}),
    "unknown claude key": _manifest_with(("claude", "primary"), "opus"),
    "unknown codex key": _manifest_with(("codex", "effort"), "high"),
    "unknown superseded key": _manifest_with(("superseded", "codex.speed"), []),
    "wrong schema version": _manifest_with(("schema_version",), 2),
    "boolean schema version": _manifest_with(("schema_version",), True),
    "float schema version": _manifest_with(("schema_version",), 1.0),
    "bad claude alias": _manifest_with(("claude", "main"), "gpt-4"),
    "quote injection": _manifest_with(("claude", "agents", "alpha"), 'opus"'),
    "newline injection": _manifest_with(
        ("claude", "agents", "alpha"), "opus\ntools: Bash"
    ),
    "trailing newline": _manifest_with(("claude", "main"), "opus\n"),
    "space in codex model": _manifest_with(("codex", "model"), "gpt 9"),
    "toml injection": _manifest_with(("codex", "model"), 'x"\nsandbox_mode = "y'),
    "effort outside the enum": _manifest_with(("codex", "reasoning_effort"), "extreme"),
    "non-string value": _manifest_with(("codex", "model"), 9),
    "null agent model": _manifest_with(("claude", "agents", "alpha"), None),
    "null codex model": _manifest_with(("codex", "model"), None),
    "agent name leaves the directory": _manifest_with(
        ("claude", "agents", "../../evil"), "opus"
    ),
    "invalid superseded entry": _manifest_with(
        ("superseded", "codex.reasoning_effort"), ["extreme"]
    ),
    "superseded not a list": _manifest_with(("superseded", "codex.model"), "gpt-8-old"),
}


@pytest.mark.parametrize("name", sorted(INVALID_MANIFESTS))
def test_invalid_manifest_is_rejected_before_anything_is_written(
    name: str, tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, MANIFEST_REL, canonical(INVALID_MANIFESTS[name]))
    # Something would be rewritten if the manifest were accepted.
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    before = snapshot(tmp_path)

    for command in ("apply", "check"):
        result = run(tmp_path, command)

        assert result.returncode == 1, (name, command, result.stdout)
        assert payload_of(result)["ok"] is False
    assert snapshot(tmp_path) == before


def test_missing_required_key_is_rejected(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    manifest = copy.deepcopy(FIXTURE_MANIFEST)
    del manifest["codex"]["reasoning_effort"]
    write(tmp_path, MANIFEST_REL, canonical(manifest))

    result = run(tmp_path, "check")

    assert result.returncode == 1
    assert "codex.reasoning_effort" in payload_of(result)["error"]


def test_duplicate_manifest_key_is_rejected(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    text = canonical(FIXTURE_MANIFEST).replace(
        '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,'
    )
    write(tmp_path, MANIFEST_REL, text)

    result = run(tmp_path, "check")

    assert result.returncode == 1
    assert "duplicate key" in payload_of(result)["error"]


def test_missing_manifest_is_an_error_not_an_empty_result(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    (tmp_path / MANIFEST_REL).unlink()

    result = run(tmp_path, "check")

    assert result.returncode == 1
    assert payload_of(result)["ok"] is False


def test_nullable_manifest_keys_are_left_unmanaged(tmp_path: Path, run: Runner) -> None:
    manifest = _manifest_with(("claude", "main"), None)
    make_project(tmp_path, manifest)
    settings = copy.deepcopy(FIXTURE_SETTINGS)
    settings["model"] = "whatever-the-user-picked"
    write(tmp_path, SETTINGS_REL, canonical(settings))
    before = snapshot(tmp_path)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout
    assert payload_of(result)["custom"] == []
    assert snapshot(tmp_path) == before


# --- get / set ----------------------------------------------------------------


def test_get_returns_the_resolved_value(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)

    result = run(tmp_path, "get", "claude.agents.beta")

    assert result.returncode == 0, result.stdout
    assert payload_of(result) == {
        "ok": True,
        "key": "claude.agents.beta",
        "value": "fable",
        "source": "manifest",
    }


def test_get_prefers_the_local_override(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    write(
        tmp_path,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "codex": {"model": "gpt-local"}}),
    )

    result = run(tmp_path, "get", "codex.model")

    assert payload_of(result)["value"] == "gpt-local"
    assert payload_of(result)["source"] == "local"


def test_get_unknown_key_is_bad_args(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)

    result = run(tmp_path, "get", "codex.modle")

    assert result.returncode == 1
    assert "unknown key" in payload_of(result)["error"]


def test_set_rewrites_the_manifest_and_records_the_outgoing_value(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)

    result = run(tmp_path, "set", "codex.model", "gpt-9.2-test")

    assert result.returncode == 0, result.stdout
    payload = payload_of(result)
    assert payload["result"] == "applied"
    assert payload["artifacts"] == [MANIFEST_REL]
    expected = _manifest_with(("codex", "model"), "gpt-9.2-test")
    # The outgoing value joins the history, so installs still on it advance.
    expected["superseded"]["codex.model"] = ["gpt-8-old", "gpt-9.1-test"]
    assert read(tmp_path, MANIFEST_REL) == canonical(expected)
    # set edits the manifest only; the copies are apply's job.
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG


def test_set_then_apply_is_a_one_file_bump(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)

    assert run(tmp_path, "set", "codex.model", "gpt-9.2-test").returncode == 0
    assert run(tmp_path, "check").returncode == 2
    assert run(tmp_path, "apply").returncode == 0

    assert run(tmp_path, "check").returncode == 0
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        'model = "gpt-9.1-test"', 'model = "gpt-9.2-test"'
    )


def test_set_to_the_current_value_is_a_no_op(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    before = snapshot(tmp_path)

    result = run(tmp_path, "set", "codex.reasoning_effort", "high")

    assert result.returncode == 0
    assert payload_of(result)["result"] == "no-op"
    assert snapshot(tmp_path) == before


def test_set_back_to_an_old_value_removes_it_from_the_history(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)

    assert run(tmp_path, "set", "codex.reasoning_effort", "medium").returncode == 0

    manifest = json.loads(read(tmp_path, MANIFEST_REL))
    assert manifest["codex"]["reasoning_effort"] == "medium"
    assert manifest["superseded"]["codex.reasoning_effort"] == ["high"]


def test_set_an_agent_model_records_no_history(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)

    assert run(tmp_path, "set", "claude.agents.alpha", "fable").returncode == 0

    manifest = json.loads(read(tmp_path, MANIFEST_REL))
    assert manifest["claude"]["agents"]["alpha"] == "fable"
    assert manifest["superseded"] == FIXTURE_MANIFEST["superseded"]


def test_set_null_is_allowed_only_where_the_schema_allows_it(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)

    assert run(tmp_path, "set", "claude.main", "null").returncode == 0
    assert json.loads(read(tmp_path, MANIFEST_REL))["claude"]["main"] is None

    before = snapshot(tmp_path)
    refused = run(tmp_path, "set", "codex.model", "null")
    assert refused.returncode == 1
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("codex.model", 'x"\nsandbox_mode = "danger'),
        ("codex.reasoning_effort", "extreme"),
        ("claude.main", "gpt-9"),
        ("claude.agents.alpha", "opus\ntools: Bash"),
        ("claude.agents.../evil", "opus"),
        ("superseded.codex.model", "gpt-8-old"),
        ("codex.modle", "gpt-9"),
    ],
)
def test_set_rejects_an_invalid_key_or_value_without_writing(
    key: str, value: str, tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    before = snapshot(tmp_path)

    result = run(tmp_path, "set", key, value)

    assert result.returncode == 1, result.stdout
    assert payload_of(result)["ok"] is False
    assert snapshot(tmp_path) == before


def test_set_does_not_write_through_a_symlinked_directory(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    skills = tmp_path / ".claude" / "skills"
    shared = tmp_path / "shared-skills"
    skills.rename(shared)
    skills.symlink_to(shared, target_is_directory=True)
    manifest_before = (shared / "_shared" / "models.json").read_bytes()

    result = run(tmp_path, "set", "codex.model", "gpt-9.2-test")

    assert result.returncode == EXIT_FAILED, result.stdout
    assert payload_of(result)["ok"] is False
    assert "symlinked directory" in payload_of(result)["error"]
    assert (shared / "_shared" / "models.json").read_bytes() == manifest_before
    assert sorted(path.name for path in (shared / "_shared").iterdir()) == [
        "models.json"
    ]


# --- apply: the happy path ----------------------------------------------------


def test_apply_renders_every_location_and_is_idempotent(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    write(
        tmp_path,
        SETTINGS_REL,
        canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")),
    )
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"').replace(
            'effort = "high"', 'effort = "medium"'
        ),
    )

    first = run(tmp_path, "apply")

    assert first.returncode == 0, first.stdout + first.stderr
    payload = payload_of(first)
    assert payload["ok"] is True
    assert payload["result"] == "applied"
    assert sorted(payload["artifacts"]) == [
        ".claude/agents/alpha.md",
        ".claude/agents/beta.md",
        SETTINGS_REL,
        CONFIG_REL,
    ]
    # Exactly the pristine fixture again: only the managed values moved, and
    # the prose line that merely looks like frontmatter was left alone.
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "opus")
    assert read(tmp_path, ".claude/agents/beta.md") == agent_text("beta", "fable")
    assert read(tmp_path, SETTINGS_REL) == canonical(FIXTURE_SETTINGS)
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG
    assert 'changed: .codex/config.toml model: "gpt-8-old" -> "gpt-9.1-test"' in (
        first.stderr
    )

    after_first = snapshot(tmp_path)
    second = run(tmp_path, "apply")

    assert second.returncode == 0
    assert payload_of(second)["result"] == "no-op"
    assert payload_of(second)["artifacts"] == []
    assert snapshot(tmp_path) == after_first
    assert run(tmp_path, "check").returncode == 0


def test_apply_adds_absent_keys_without_touching_the_rest(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    settings = copy.deepcopy(FIXTURE_SETTINGS)
    del settings["model"]
    del settings["env"]
    write(tmp_path, SETTINGS_REL, canonical(settings))
    config = (
        "# Codex config\n"
        'approval_policy = "never"\n'
        "\n"
        "# Feature toggles\n"
        "[features]\n"
        "skills = true\n"
    )
    write(tmp_path, CONFIG_REL, config)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    expected_settings = {
        **settings,
        "model": "fable",
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "opus"},
    }
    assert read(tmp_path, SETTINGS_REL) == canonical(expected_settings)
    assert list(json.loads(read(tmp_path, SETTINGS_REL)))[:4] == list(settings)[:4]
    # Inserted before the first table, above the comment that belongs to it.
    assert read(tmp_path, CONFIG_REL) == (
        "# Codex config\n"
        'approval_policy = "never"\n'
        "\n"
        'model = "gpt-9.1-test"\n'
        'model_reasoning_effort = "high"\n'
        "\n"
        "# Feature toggles\n"
        "[features]\n"
        "skills = true\n"
    )
    assert run(tmp_path, "apply").returncode == 0
    assert run(tmp_path, "check").returncode == 0


def test_apply_appends_absent_toml_keys_when_there_is_no_table(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, CONFIG_REL, 'model = "gpt-9.1-test"')

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, CONFIG_REL) == (
        'model = "gpt-9.1-test"\nmodel_reasoning_effort = "high"\n'
    )


def test_apply_keeps_toml_spacing_comments_and_line_endings(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    config = (
        "# pinned by the template\r\n"
        'model   =  "gpt-8-old"   # the Codex model\r\n'
        'model_reasoning_effort="medium"\r\n'
        "\r\n"
        "[features]\r\n"
        "skills = true\r\n"
    )
    write(tmp_path, CONFIG_REL, config)

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, CONFIG_REL) == (
        "# pinned by the template\r\n"
        'model   =  "gpt-9.1-test"   # the Codex model\r\n'
        'model_reasoning_effort="high"\r\n'
        "\r\n"
        "[features]\r\n"
        "skills = true\r\n"
    )


def test_apply_keeps_a_quoted_frontmatter_value_quoted(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    quoted = agent_text("alpha", "sonnet").replace(
        "model: sonnet\n---", 'model: "sonnet"\n---'
    )
    write(tmp_path, ".claude/agents/alpha.md", quoted)

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, ".claude/agents/alpha.md") == quoted.replace(
        'model: "sonnet"\n---', 'model: "opus"\n---'
    )


def test_brackets_in_toml_strings_and_comments_are_not_structure(
    tmp_path: Path, run: Runner
) -> None:
    """The mirror image of the refusals below: a bracket that is only text
    must not make an ordinary line look unfinished."""
    make_project(tmp_path)
    config = (
        'pattern = "[a-z"  # see [the docs\n'
        "notify = ['say', \"it's ]\"]\n"
        'model = "gpt-8-old"\n'
        "\n"
        "[features]\n"
        "skills = true\n"
    )
    write(tmp_path, CONFIG_REL, config)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert read(tmp_path, CONFIG_REL) == (
        'pattern = "[a-z"  # see [the docs\n'
        "notify = ['say', \"it's ]\"]\n"
        'model = "gpt-9.1-test"\n'
        "\n"
        'model_reasoning_effort = "high"\n'
        "\n"
        "[features]\n"
        "skills = true\n"
    )


def test_apply_ignores_a_model_key_inside_a_toml_table(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)

    assert run(tmp_path, "apply").returncode == 0

    assert 'model = "gpt-profile-model"' in read(tmp_path, CONFIG_REL)


def test_apply_preserves_the_file_mode_and_leaves_no_temporary_files(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(
        tmp_path,
        SETTINGS_REL,
        canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")),
    )
    # Group-writable: a mode the usual umask strips from a newly created file,
    # so it only survives if the tool restores it explicitly.
    restricted = 0o664
    (tmp_path / SETTINGS_REL).chmod(restricted)
    names_before = set(snapshot(tmp_path))

    assert run(tmp_path, "apply").returncode == 0

    assert stat.S_IMODE((tmp_path / SETTINGS_REL).stat().st_mode) == restricted
    assert set(snapshot(tmp_path)) == names_before


def test_apply_skips_native_files_that_do_not_exist(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    (tmp_path / SETTINGS_REL).unlink()
    (tmp_path / CONFIG_REL).unlink()
    names_before = set(snapshot(tmp_path))

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout
    assert {entry["file"] for entry in payload_of(result)["skipped"]} == {
        SETTINGS_REL,
        CONFIG_REL,
    }
    # A settings file is never created on the project's behalf.
    assert set(snapshot(tmp_path)) == names_before


# --- apply: the advance-if-not-custom rule ------------------------------------


def test_apply_advances_values_an_earlier_template_shipped(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    settings = settings_with(
        CLAUDE_CODE_SUBAGENT_MODEL="sonnet", CODEX_MODEL="gpt-8-old"
    )
    settings["model"] = "haiku"
    settings["language"] = "english"
    write(tmp_path, SETTINGS_REL, canonical(settings))
    config = (
        FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"')
        .replace('effort = "high"', 'effort = "medium"')
        .replace('approval_policy = "never"', 'approval_policy = "on-request"')
    )
    write(tmp_path, CONFIG_REL, config)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload_of(result)["custom"] == []
    # The project's unrelated edits are byte-identical; only shipped defaults
    # moved, and the shadowing env.CODEX_MODEL is gone.
    expected_settings = copy.deepcopy(FIXTURE_SETTINGS)
    expected_settings["language"] = "english"
    assert read(tmp_path, SETTINGS_REL) == canonical(expected_settings)
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        'approval_policy = "never"', 'approval_policy = "on-request"'
    )


def test_apply_removes_a_codex_model_env_equal_to_the_target(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, SETTINGS_REL, canonical(settings_with(CODEX_MODEL="gpt-9.1-test")))

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, SETTINGS_REL) == canonical(FIXTURE_SETTINGS)


def test_apply_keeps_custom_values_and_warns(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    settings = settings_with(
        CLAUDE_CODE_SUBAGENT_MODEL="haiku", CODEX_MODEL="gpt-my-own-pin"
    )
    settings["model"] = "claude-opus-9-custom"
    write(tmp_path, SETTINGS_REL, canonical(settings))
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-my-own-pin"').replace(
            'effort = "high"', 'effort = "low"'
        ),
    )
    before = snapshot(tmp_path)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert snapshot(tmp_path) == before
    kept = {
        (note["file"], note["key"]): note["value"]
        for note in payload_of(result)["custom"]
    }
    assert kept == {
        (SETTINGS_REL, "model"): "claude-opus-9-custom",
        (SETTINGS_REL, "env.CLAUDE_CODE_SUBAGENT_MODEL"): "haiku",
        (SETTINGS_REL, "env.CODEX_MODEL"): "gpt-my-own-pin",
        (CONFIG_REL, "model"): "gpt-my-own-pin",
        (CONFIG_REL, "model_reasoning_effort"): "low",
    }
    assert result.stderr.count("custom: ") == len(kept)
    assert "models.local.json" in result.stderr
    # A deliberate value is not drift: the gate stays green.
    check = run(tmp_path, "check")
    assert check.returncode == 0, check.stdout
    assert len(payload_of(check)["custom"]) == len(kept)


def test_apply_advances_one_key_while_keeping_a_custom_neighbour(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"').replace(
            'effort = "high"', 'effort = "low"'
        ),
    )

    result = run(tmp_path, "apply")

    assert result.returncode == 0
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        'effort = "high"', 'effort = "low"'
    )
    assert [note["key"] for note in payload_of(result)["custom"]] == [
        "model_reasoning_effort"
    ]


# --- apply: refused layouts, all-or-nothing -----------------------------------


def _compact_settings(root: Path) -> None:
    write(
        root,
        SETTINGS_REL,
        json.dumps(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")),
    )


def _duplicate_json_key(root: Path) -> None:
    text = canonical(FIXTURE_SETTINGS).replace(
        '  "model": "fable"\n', '  "model": "haiku",\n  "model": "fable"\n'
    )
    assert text != canonical(FIXTURE_SETTINGS)
    write(root, SETTINGS_REL, text)


def _symlinked_settings(root: Path) -> None:
    real = write(root, "elsewhere/settings.json", canonical(settings_with()))
    (root / SETTINGS_REL).unlink()
    (root / SETTINGS_REL).symlink_to(real)


def _symlinked_directory(rel: str) -> Callable[[Path], object]:
    """Replace a directory with a link to a copy of it outside the tree's own
    layout, the way a shared configuration directory is linked into a project."""

    def mutate(root: Path) -> None:
        directory = root / rel
        moved = root / "shared" / directory.name
        moved.parent.mkdir(parents=True, exist_ok=True)
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)

    return mutate


def _nan_settings(root: Path) -> None:
    settings = settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")
    settings["unused"] = float("nan")
    text = canonical(settings)
    assert "NaN" in text
    write(root, SETTINGS_REL, text)


HIDDEN_MODEL_AGENT = (
    "---\n"
    "name: alpha\n"
    'description: "A description that continues\n'
    "model: sonnet\n"
    'note: on a third line"\n'
    "tools: Read\n"
    "---\n"
    "\n"
    "Body.\n"
)


def _toml(old: str, new: str) -> Callable[[Path], object]:
    def mutate(root: Path) -> None:
        assert old in FIXTURE_CONFIG
        write(root, CONFIG_REL, FIXTURE_CONFIG.replace(old, new))

    return mutate


def _agent(text: str) -> Callable[[Path], object]:
    return lambda root: write(root, ".claude/agents/alpha.md", text)


MODEL_LINE = 'model = "gpt-9.1-test"\n'
REFUSED_LAYOUTS: dict[str, tuple[Callable[[Path], object], str]] = {
    "json reformatted": (_compact_settings, SETTINGS_REL),
    "json duplicate key": (_duplicate_json_key, SETTINGS_REL),
    "json invalid": (
        lambda root: write(root, SETTINGS_REL, "{"),
        SETTINGS_REL,
    ),
    "json top-level array": (
        lambda root: write(root, SETTINGS_REL, "[]\n"),
        SETTINGS_REL,
    ),
    "json env not an object": (
        lambda root: write(root, SETTINGS_REL, canonical({"env": "nope"})),
        SETTINGS_REL,
    ),
    "settings is a symlink": (_symlinked_settings, SETTINGS_REL),
    "json NaN": (_nan_settings, SETTINGS_REL),
    "codex directory is a symlink": (_symlinked_directory(".codex"), CONFIG_REL),
    "agents directory is a symlink": (
        _symlinked_directory(".claude/agents"),
        ".claude/agents/alpha.md",
    ),
    # `[ # ]` is an array that is still open: the `]` is inside a comment. Read
    # as closed, the next line passes for a table header and a key is inserted
    # in the middle of the array.
    "toml bracket inside a comment": (
        _toml(MODEL_LINE, "matrix = [ # ]\n[1]\n]\n"),
        CONFIG_REL,
    ),
    "toml bracket inside a string": (
        _toml(MODEL_LINE, 'matrix = [ "]"\n[1]\n]\n'),
        CONFIG_REL,
    ),
    # A table of the same name makes a top-level scalar a duplicate definition,
    # whether or not the scalar is already there.
    "toml table named like a managed key": (
        _toml("[profiles.fast]\n", '[model]\nalias = "local"\n\n[profiles.fast]\n'),
        CONFIG_REL,
    ),
    "toml array table named like an absent managed key": (
        lambda root: write(
            root,
            CONFIG_REL,
            'approval_policy = "never"\n\n[features]\nskills = true\n\n'
            '[[model]]\nalias = "local"\n',
        ),
        CONFIG_REL,
    ),
    "agent description hides a model line": (
        _agent(HIDDEN_MODEL_AGENT),
        ".claude/agents/alpha.md",
    ),
    # The quoted value opens on an indented line and its continuation sits at
    # column 0, where it reads exactly like the model key.
    "agent indented description hides a model line": (
        _agent(
            "---\nname: alpha\ndescription:\n"
            '  "first\nmodel: sonnet\nnote: last"\n---\n\nBody.\n'
        ),
        ".claude/agents/alpha.md",
    ),
    # Replacing `>-` would leave the old value behind as a continuation line.
    "agent model is a block scalar": (
        _agent("---\nname: alpha\nmodel: >-\n  sonnet\n---\n\nBody.\n"),
        ".claude/agents/alpha.md",
    ),
    # An alias, a tag, or an anchor is not a name to substitute over.
    "agent model is a yaml alias": (
        _agent("---\nname: alpha\nmodel: *shared\n---\n\nBody.\n"),
        ".claude/agents/alpha.md",
    ),
    # YAML folds a plain scalar across blank lines: the value here is
    # "sonnet suffix", and replacing the first word would leave "opus suffix".
    "agent model continues after a blank line": (
        _agent("---\nname: alpha\nmodel: sonnet\n\n  suffix\n---\n\nBody.\n"),
        ".claude/agents/alpha.md",
    ),
    "agent flow list not closed": (
        _agent(
            agent_text("alpha", "opus").replace("tools: Read, Grep", "tools: [Read,")
        ),
        ".claude/agents/alpha.md",
    ),
    "agent top-level line without a key": (
        _agent(agent_text("alpha", "opus").replace("tools: Read, Grep", "just text")),
        ".claude/agents/alpha.md",
    ),
    "toml unbalanced closer": (_toml(MODEL_LINE, "x = ]\n" + MODEL_LINE), CONFIG_REL),
    "toml bracket line that is not a header": (
        _toml(MODEL_LINE, "[features\n" + MODEL_LINE),
        CONFIG_REL,
    ),
    # Invalid TOML that a parser would reject. It is refused by the scan, so
    # the answer is the same on an interpreter that has no parser.
    "toml duplicate unmanaged key": (
        _toml(
            'approval_policy = "never"\n',
            'approval_policy = "never"\napproval_policy = "on-request"\n',
        ),
        CONFIG_REL,
    ),
    "config is not utf-8": (
        lambda root: (root / CONFIG_REL).write_bytes(b'model = "caf\xe9"\n'),
        CONFIG_REL,
    ),
    "settings is a directory": (
        lambda root: ((root / SETTINGS_REL).unlink(), (root / SETTINGS_REL).mkdir()),
        SETTINGS_REL,
    ),
    "agent model continues on the next line": (
        _agent("---\nname: alpha\nmodel: sonnet\n  extra\n---\n\nBody.\n"),
        ".claude/agents/alpha.md",
    ),
    # `\u0064` is `d`: this header defines a table called model.
    "toml table name hidden behind an escape": (
        _toml("[profiles.fast]\n", '["mo\\u0064el"]\nalias = "x"\n\n[profiles.fast]\n'),
        CONFIG_REL,
    ),
    "toml quoted table named like a managed key": (
        _toml("[profiles.fast]\n", "['model']\nalias = \"x\"\n\n[profiles.fast]\n"),
        CONFIG_REL,
    ),
    "json number out of range": (
        lambda root: write(
            root,
            SETTINGS_REL,
            canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")).replace(
                '"language": "japanese"', '"language": "japanese",\n  "big": 1e400'
            ),
        ),
        SETTINGS_REL,
    ),
    "toml single-quoted value": (
        _toml(MODEL_LINE, "model = 'gpt-8-old'\n"),
        CONFIG_REL,
    ),
    "toml quoted key": (_toml(MODEL_LINE, '"model" = "gpt-8-old"\n'), CONFIG_REL),
    "toml dotted key": (_toml(MODEL_LINE, 'model.name = "gpt-8-old"\n'), CONFIG_REL),
    "toml indented key": (_toml(MODEL_LINE, '  model = "gpt-8-old"\n'), CONFIG_REL),
    "toml escaped value": (_toml(MODEL_LINE, 'model = "gpt\\u002d8"\n'), CONFIG_REL),
    "toml duplicate key": (
        _toml(MODEL_LINE, MODEL_LINE + 'model = "gpt-8-old"\n'),
        CONFIG_REL,
    ),
    # Every line here looks like a plain assignment, and the middle one holds
    # a shipped default, but it is the *content* of a string. Only the
    # multi-line rule stands between this file and an edit inside that string
    # on an interpreter without tomllib.
    "toml multi-line string": (
        _toml(MODEL_LINE, 'notes = """\nmodel = "gpt-8-old"\nx = """\n'),
        CONFIG_REL,
    ),
    "toml multi-line array": (
        _toml(MODEL_LINE, 'notify = [\n  "a",\n  ["b"]\n]\n' + MODEL_LINE),
        CONFIG_REL,
    ),
    "agent with two model lines": (
        _agent(agent_text("alpha", "opus").replace("tools:", "model: haiku\ntools:")),
        ".claude/agents/alpha.md",
    ),
    "agent without a model line": (
        _agent(agent_text("alpha", "opus").replace("model: opus\n---", "---")),
        ".claude/agents/alpha.md",
    ),
    "agent without frontmatter": (
        _agent("model: sonnet\n"),
        ".claude/agents/alpha.md",
    ),
    "agent with an unclosed frontmatter": (
        _agent("---\nname: alpha\nmodel: sonnet\n"),
        ".claude/agents/alpha.md",
    ),
    "agent with a commented model line": (
        _agent(
            agent_text("alpha", "opus").replace(
                "model: opus\n---", "model: opus # x\n---"
            )
        ),
        ".claude/agents/alpha.md",
    ),
    "agent file without a manifest entry": (
        lambda root: write(
            root, ".claude/agents/gamma.md", agent_text("gamma", "opus")
        ),
        ".claude/agents/gamma.md",
    ),
    "manifest entry without an agent file": (
        lambda root: (root / ".claude/agents/beta.md").unlink(),
        ".claude/agents/beta.md",
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSED_LAYOUTS))
def test_one_refused_file_means_nothing_is_written_anywhere(
    name: str, tmp_path: Path, run: Runner
) -> None:
    mutate, refused_file = REFUSED_LAYOUTS[name]
    make_project(tmp_path)
    # Every other location needs an edit, so a partial apply would be visible.
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    write(
        tmp_path,
        SETTINGS_REL,
        canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")),
    )
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('effort = "high"', 'effort = "medium"'),
    )
    mutate(tmp_path)
    before = snapshot(tmp_path)

    result = run(tmp_path, "apply")

    assert result.returncode == EXIT_REFUSED, (name, result.stdout, result.stderr)
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["result"] == "refused"
    # A refusal is not a failure, and must not be reported like one.
    assert "error:" not in result.stderr
    assert payload["changes"] == []
    assert payload["artifacts"] == []
    assert refused_file in [entry["file"] for entry in payload["refused"]]
    assert f"refused: {refused_file}" in result.stderr
    assert "nothing was written" in result.stderr
    assert snapshot(tmp_path) == before

    check = run(tmp_path, "check")
    assert check.returncode == 2, (name, check.stdout)
    assert snapshot(tmp_path) == before


def test_only_top_level_frontmatter_model_lines_count(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    nested = agent_text("alpha", "sonnet").replace(
        "tools: Read, Grep\n",
        "tools: [Read, Grep]\nhooks:\n  model: nested-value\nnotes: >\n  model: prose\n",
    )
    assert nested != agent_text("alpha", "sonnet")
    write(tmp_path, ".claude/agents/alpha.md", nested)

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, ".claude/agents/alpha.md") == nested.replace(
        "model: sonnet\n---", "model: opus\n---"
    )


ORDINARY_FRONTMATTER = {
    "list entries at column zero": "skills:\n- repo-review\n- lint\n",
    "list entries with mappings": "hooks:\n  - matcher: Bash\n    model: nested\n",
    "flow list with a hash inside an item": "tools: [Bash(echo a#b), Read]\n",
    "block scalar holding quotes and brackets": (
        'notes: |\n  "an unfinished quote\n  [and an open bracket\n  model: prose\n'
    ),
    "folded scalar with indicators": "notes: >-\n  model: prose\n\n  more\n",
    "comment and blank lines": "# model: haiku\n\ncolor: blue  # trailing\n",
    "quoted values that close": "a: \"x: [y\"\nb: 'it''s # fine'\n",
}


@pytest.mark.parametrize("name", sorted(ORDINARY_FRONTMATTER))
def test_ordinary_frontmatter_is_edited_on_the_model_line_only(
    name: str, tmp_path: Path, run: Runner
) -> None:
    """The refusal rules must not block an agent written in everyday YAML:
    that would stop every update of a project that merely has such a file."""
    make_project(tmp_path)
    text = agent_text("alpha", "sonnet").replace(
        "tools: Read, Grep\n", ORDINARY_FRONTMATTER[name]
    )
    assert text != agent_text("alpha", "sonnet")
    write(tmp_path, ".claude/agents/alpha.md", text)

    result = run(tmp_path, "apply")

    assert result.returncode == 0, (name, result.stderr)
    assert read(tmp_path, ".claude/agents/alpha.md") == text.replace(
        "model: sonnet\n---", "model: opus\n---"
    )


def test_a_quoted_table_name_that_only_starts_like_a_managed_key_is_fine(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    config = FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"') + (
        '\n["model.foo"]\na = 1\n\n[mcp_servers."my server"]\ncommand = "x"\n'
    )
    write(tmp_path, CONFIG_REL, config)

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, CONFIG_REL) == config.replace('"gpt-8-old"', '"gpt-9.1-test"')


def test_an_unmanaged_agent_may_omit_its_model_line(
    tmp_path: Path, run: Runner
) -> None:
    """A null leaf means "not managed", so the file is not held to the layout
    a managed one needs: it may inherit its model and carry no line at all."""
    make_project(tmp_path)
    inheriting = agent_text("alpha", "opus").replace("model: opus\n---", "---")
    write(tmp_path, ".claude/agents/alpha.md", inheriting)
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    write(
        tmp_path,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "claude": {"agents": {"alpha": None}}}),
    )

    assert run(tmp_path, "apply").returncode == 0

    assert read(tmp_path, ".claude/agents/alpha.md") == inheriting
    assert read(tmp_path, ".claude/agents/beta.md") == agent_text("beta", "fable")
    assert run(tmp_path, "check").returncode == 0


def test_a_reported_value_cannot_start_a_line_of_its_own(
    tmp_path: Path, run: Runner
) -> None:
    """The shell callers route each stderr line by its prefix. A project value
    holding a line break must not be able to forge an `error:` line and turn a
    successful apply into a reported failure."""
    make_project(tmp_path)
    settings = copy.deepcopy(FIXTURE_SETTINGS)
    settings["model"] = "local\nerror: forged by a value\r\nchanged: also forged"
    write(tmp_path, SETTINGS_REL, canonical(settings))

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout
    lines = result.stderr.splitlines()
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"custom: {SETTINGS_REL} model: kept ")
    assert payload_of(result)["custom"][0]["value"] == settings["model"]


def test_a_reported_file_name_cannot_start_a_line_of_its_own(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/odd\nerror: forged.md", agent_text("odd", "opus"))

    result = run(tmp_path, "apply")

    assert result.returncode == EXIT_REFUSED, result.stdout
    prefixes = {line.split(":", 1)[0] for line in result.stderr.splitlines()}
    assert prefixes == {"refused", "hint"}, result.stderr


def test_refusal_names_the_exact_edit_for_a_reformatted_settings_file(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    _compact_settings(tmp_path)

    result = run(tmp_path, "apply")

    reason = payload_of(result)["refused"][0]["reason"]
    assert 'set env.CLAUDE_CODE_SUBAGENT_MODEL to "opus"' in reason


def test_a_reformatted_but_correct_settings_file_is_accepted(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, SETTINGS_REL, json.dumps(FIXTURE_SETTINGS))
    before = snapshot(tmp_path)

    assert run(tmp_path, "apply").returncode == 0
    assert run(tmp_path, "check").returncode == 0
    assert snapshot(tmp_path) == before


def _load_and_break(
    models_module: ModuleType, monkeypatch: pytest.MonkeyPatch, fail_on_call: int
) -> None:
    """Make the tool's Nth atomic rename fail, as a full disk or a crash would."""
    real_replace = os.replace
    calls = {"count": 0}

    def failing_replace(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] == fail_on_call:
            raise OSError("simulated rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(models_module.os, "replace", failing_replace)


def test_a_write_failure_midway_restores_the_files_already_replaced(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('effort = "high"', 'effort = "medium"'),
    )
    before = snapshot(tmp_path)
    # Three files are pending; the second rename fails after the first landed.
    _load_and_break(models_module, monkeypatch, fail_on_call=2)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    # The failure signal the shell callers act on: its own exit code, with the
    # reason on an `error:` line.
    assert exit_code == EXIT_FAILED
    assert exit_code != EXIT_REFUSED
    assert [line for line in captured.err.splitlines() if line.startswith("error: ")]
    assert payload["ok"] is False
    assert payload["result"] == "failed"
    assert payload["artifacts"] == []
    assert "restored" in payload["error"]
    assert snapshot(tmp_path) == before


def test_an_interrupt_midway_restores_the_files_already_replaced(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    before = snapshot(tmp_path)
    real_replace = os.replace
    calls = {"count": 0}

    def interrupted_replace(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise KeyboardInterrupt
        real_replace(source, destination)

    monkeypatch.setattr(models_module.os, "replace", interrupted_replace)

    # The interrupt is passed on once the files are consistent again: as an
    # ordinary exit code it would let a calling script carry on.
    with pytest.raises(KeyboardInterrupt):
        models_module.main(["--project-root", str(tmp_path), "apply"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["result"] == "interrupted"
    assert "interrupted" in payload["error"]
    assert "every file was restored" in payload["error"]
    assert captured.err.startswith("error: ")
    assert snapshot(tmp_path) == before


def test_an_interrupt_right_after_a_rename_still_restores_that_file(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rename has happened but the code has not moved on: the file must
    already be on the list of what to put back."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    before = snapshot(tmp_path)
    real_replace = os.replace
    calls = {"count": 0}

    def replace_then_interrupt(source: Any, destination: Any) -> None:
        calls["count"] += 1
        real_replace(source, destination)
        if calls["count"] == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(models_module.os, "replace", replace_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        models_module.main(["--project-root", str(tmp_path), "apply"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "interrupted"
    assert "restored" in payload["error"]
    assert snapshot(tmp_path) == before


def test_an_interrupt_while_staging_leaves_no_temporary_file(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    before = snapshot(tmp_path)

    def interrupted_fsync(descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(models_module.os, "fsync", interrupted_fsync)

    with pytest.raises(KeyboardInterrupt):
        models_module.main(["--project-root", str(tmp_path), "apply"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "interrupted"
    assert "nothing was written" in payload["error"]
    assert snapshot(tmp_path) == before


def test_a_cleanup_failure_does_not_prevent_the_rollback(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory that turns read-only fails the rename and then the removal
    of the temporary file. The second failure must not skip putting the first
    file back."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('effort = "high"', 'effort = "medium"'),
    )
    alpha_before = read(tmp_path, ".claude/agents/alpha.md")
    config_before = read(tmp_path, CONFIG_REL)
    real_replace = os.replace
    real_unlink = Path.unlink
    calls = {"count": 0}

    def failing_replace(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise PermissionError("simulated read-only directory")
        real_replace(source, destination)

    def failing_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if "models-tmp" in self.name and self.parent.name == ".codex":
            raise PermissionError("simulated read-only directory")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(models_module.os, "replace", failing_replace)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_FAILED
    assert read(tmp_path, ".claude/agents/alpha.md") == alpha_before
    assert read(tmp_path, CONFIG_REL) == config_before
    assert "every file was restored" in payload["error"]
    assert "left behind" in payload["error"]


def test_a_file_saved_during_the_write_is_not_overwritten(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Someone saves a second pending file while the first is being renamed.
    Their content must survive, and the first file must go back as it was."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    alpha_before = read(tmp_path, ".claude/agents/alpha.md")
    saved_meanwhile = agent_text("beta", "haiku")
    real_replace = os.replace
    calls = {"count": 0}

    def replace_then_edit(source: Any, destination: Any) -> None:
        calls["count"] += 1
        real_replace(source, destination)
        if calls["count"] == 1:
            write(tmp_path, ".claude/agents/beta.md", saved_meanwhile)

    monkeypatch.setattr(models_module.os, "replace", replace_then_edit)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_FAILED
    assert payload["result"] == "failed"
    assert read(tmp_path, ".claude/agents/beta.md") == saved_meanwhile
    assert read(tmp_path, ".claude/agents/alpha.md") == alpha_before
    assert not [name for name in snapshot(tmp_path) if "models-tmp" in name]


def test_a_staging_failure_writes_nothing(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('effort = "high"', 'effort = "medium"'),
    )
    before = snapshot(tmp_path)
    real_open = os.open

    def failing_open(path: Any, flags: int, mode: int = 0o777) -> int:
        if "config.toml" in str(path):
            raise OSError("simulated full disk")
        return real_open(path, flags, mode)

    monkeypatch.setattr(models_module.os, "open", failing_open)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == EXIT_FAILED
    assert payload["result"] == "failed"
    assert "nothing was written" in payload["error"]
    assert captured.err.startswith("error: nothing was written")
    assert snapshot(tmp_path) == before


# --- the project's local override ---------------------------------------------


def test_local_override_replaces_single_keys(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    override = {
        "schema_version": 1,
        "claude": {"main": "opus", "agents": {"beta": "sonnet"}},
        "codex": {"model": "gpt-local-choice"},
    }
    write(tmp_path, LOCAL_REL, json.dumps(override))

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert read(tmp_path, ".claude/agents/beta.md") == agent_text("beta", "sonnet")
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "opus")
    # The files held the manifest's own values. Those are template defaults,
    # not project choices, so the override replaces them without a warning.
    expected_settings = {**FIXTURE_SETTINGS, "model": "opus"}
    assert read(tmp_path, SETTINGS_REL) == canonical(expected_settings)
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        'model = "gpt-9.1-test"', 'model = "gpt-local-choice"'
    )
    assert payload_of(result)["custom"] == []
    # The override file itself is never rewritten.
    assert json.loads(read(tmp_path, LOCAL_REL)) == override
    assert run(tmp_path, "check").returncode == 0


def test_a_project_assigns_its_own_agent_in_the_local_override(
    tmp_path: Path, run: Runner
) -> None:
    """An agent the template does not ship would otherwise fail the gate with
    no fix that survives the next update."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/gamma.md", agent_text("gamma", "haiku"))
    assert run(tmp_path, "check").returncode == 2

    write(
        tmp_path,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "claude": {"agents": {"gamma": "sonnet"}}}),
    )

    assert run(tmp_path, "apply").returncode == 0
    assert read(tmp_path, ".claude/agents/gamma.md") == agent_text("gamma", "sonnet")
    assert run(tmp_path, "check").returncode == 0


def test_local_override_is_the_target_for_shipped_defaults(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(
        tmp_path,
        SETTINGS_REL,
        canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet")),
    )
    write(tmp_path, CONFIG_REL, FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"'))
    override = {
        "schema_version": 1,
        "claude": {"subagent_default": "haiku"},
        "codex": {"model": "gpt-local-choice"},
    }
    write(tmp_path, LOCAL_REL, json.dumps(override))

    assert run(tmp_path, "apply").returncode == 0

    settings = json.loads(read(tmp_path, SETTINGS_REL))
    assert settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        '"gpt-9.1-test"', '"gpt-local-choice"'
    )
    assert run(tmp_path, "check").returncode == 0


def test_recording_a_custom_value_in_the_override_silences_the_warning(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('effort = "high"', 'effort = "low"'),
    )
    assert payload_of(run(tmp_path, "apply"))["custom"] != []

    write(
        tmp_path,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "codex": {"reasoning_effort": "low"}}),
    )
    result = run(tmp_path, "apply")

    assert result.returncode == 0
    assert payload_of(result)["custom"] == []
    assert "custom:" not in result.stderr


def test_a_null_leaf_leaves_that_one_key_unmanaged(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "haiku"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    write(
        tmp_path,
        CONFIG_REL,
        FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"').replace(
            'effort = "high"', 'effort = "medium"'
        ),
    )
    override = {
        "schema_version": 1,
        "claude": {"agents": {"alpha": None}},
        "codex": {"model": None},
    }
    write(tmp_path, LOCAL_REL, json.dumps(override))

    assert run(tmp_path, "apply").returncode == 0

    # The unmanaged keys keep what they had; their managed neighbours move.
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "haiku")
    assert read(tmp_path, ".claude/agents/beta.md") == agent_text("beta", "fable")
    assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG.replace(
        '"gpt-9.1-test"', '"gpt-8-old"'
    )
    assert run(tmp_path, "check").returncode == 0


def test_opting_out_leaves_native_settings_byte_identical(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    _compact_settings(tmp_path)
    write(tmp_path, CONFIG_REL, "model = 'anything goes'\n")
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(
        tmp_path,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "manage_native_settings": False}),
    )
    native_before = (read(tmp_path, SETTINGS_REL), read(tmp_path, CONFIG_REL))

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (read(tmp_path, SETTINGS_REL), read(tmp_path, CONFIG_REL)) == native_before
    # Agent definitions are template-owned and are still rendered: an update
    # would otherwise reset them to the template's values every time.
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "opus")
    assert {entry["file"] for entry in payload_of(result)["skipped"]} == {
        SETTINGS_REL,
        CONFIG_REL,
    }
    assert run(tmp_path, "check").returncode == 0


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": 1, "superseded": {}},
        {"schema_version": 1, "codex": {"model": 'x"y'}},
        {"schema_version": 1, "manage_native_settings": "no"},
        {"schema_version": 2},
        {"claude": {"main": "opus"}},
        ["not", "an", "object"],
    ],
)
def test_invalid_local_override_stops_before_anything_is_written(
    override: Any, tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, LOCAL_REL, json.dumps(override))
    before = snapshot(tmp_path)

    result = run(tmp_path, "apply")

    assert result.returncode == 1, result.stdout
    assert payload_of(result)["ok"] is False
    assert "models.local.json" in result.stderr
    assert snapshot(tmp_path) == before


# --- check --------------------------------------------------------------------


def test_check_reports_drift_and_writes_nothing(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, SETTINGS_REL, canonical(settings_with(CODEX_MODEL="gpt-8-old")))
    before = snapshot(tmp_path)

    result = run(tmp_path, "check")

    assert result.returncode == 2, result.stdout
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["result"] == "drift"
    assert {(entry["file"], entry["key"]) for entry in payload["drift"]} == {
        (".claude/agents/alpha.md", "model"),
        (SETTINGS_REL, "env.CODEX_MODEL"),
    }
    assert 'drift: .claude/agents/alpha.md model: "sonnet" -> "opus"' in result.stderr
    assert "models.py apply" in result.stderr
    assert snapshot(tmp_path) == before


def test_check_ignores_the_process_environment(tmp_path: Path, run: Runner) -> None:
    make_project(tmp_path)
    noisy = {
        **os.environ,
        "CODEX_MODEL": "gpt-from-the-shell",
        "CLAUDE_CODE_SUBAGENT_MODEL": "haiku",
        "ANTHROPIC_MODEL": "sonnet",
    }

    result = run(tmp_path, "check", env=noisy)

    assert result.returncode == 0, result.stdout
    assert payload_of(result)["result"] == "in-sync"
    assert result.stderr == ""


def test_project_root_goes_before_the_subcommand(tmp_path: Path) -> None:
    """One position, the one every bundled helper documents. After the
    subcommand it is a usage error, reported like any other."""
    make_project(tmp_path)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "check", "--project-root", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, result.stdout
    assert payload_of(result)["ok"] is False
    assert "--project-root" in payload_of(result)["error"]


# --- strict mode: the template has no project choices ------------------------


def _custom_codex_env(root: Path) -> None:
    write(root, SETTINGS_REL, canonical(settings_with(CODEX_MODEL="gpt-my-own-pin")))


ACCEPTABLE_ONLY_IN_A_PROJECT: dict[str, Callable[[Path], object]] = {
    "a value the manifest does not name": lambda root: write(
        root, CONFIG_REL, FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-unreviewed"')
    ),
    "a custom codex model env": _custom_codex_env,
    "a missing settings file": lambda root: (root / SETTINGS_REL).unlink(),
    "an opt-out": lambda root: write(
        root,
        LOCAL_REL,
        json.dumps({"schema_version": 1, "manage_native_settings": False}),
    ),
    "an override that changes nothing": lambda root: write(
        root, LOCAL_REL, json.dumps({"schema_version": 1})
    ),
}


@pytest.mark.parametrize("name", sorted(ACCEPTABLE_ONLY_IN_A_PROJECT))
def test_strict_check_fails_on_what_only_a_project_may_have(
    name: str, tmp_path: Path, run: Runner
) -> None:
    """The same state passes in an installed project and fails in the
    template, where it can only be a hand edit the manifest never named."""
    make_project(tmp_path)
    ACCEPTABLE_ONLY_IN_A_PROJECT[name](tmp_path)
    before = snapshot(tmp_path)

    lenient = run(tmp_path, "check")
    strict = run(tmp_path, "check", "--strict")

    assert lenient.returncode == 0, (name, lenient.stdout)
    assert payload_of(lenient)["strict_findings"] == []
    assert strict.returncode == 2, (name, strict.stdout)
    payload = payload_of(strict)
    assert payload["ok"] is False
    assert payload["strict"] is True
    assert payload["strict_findings"], name
    assert "strict: " in strict.stderr
    assert snapshot(tmp_path) == before


def test_strict_check_passes_when_every_location_is_the_manifest_value(
    tmp_path: Path, run: Runner
) -> None:
    make_project(tmp_path)

    result = run(tmp_path, "check", "--strict")

    assert result.returncode == 0, result.stdout
    assert payload_of(result)["result"] == "in-sync"
    assert result.stderr == ""


# --- a settings file's own ending ---------------------------------------------


@pytest.mark.parametrize("ending", ["", "\n", "\n\n"], ids=["none", "one", "two"])
def test_a_settings_file_is_edited_whatever_its_final_newline(
    ending: str, tmp_path: Path, run: Runner
) -> None:
    """A missing final newline is an ordinary file, not a reformatted one.
    Refusing it would stop every write, agent rendering included."""
    make_project(tmp_path)
    stale = canonical(settings_with(CLAUDE_CODE_SUBAGENT_MODEL="sonnet"))
    write(tmp_path, SETTINGS_REL, stale.rstrip("\n") + ending)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))

    result = run(tmp_path, "apply")

    assert result.returncode == 0, result.stdout + result.stderr
    # The file's own ending is written back byte for byte.
    assert read(tmp_path, SETTINGS_REL) == (
        canonical(FIXTURE_SETTINGS).rstrip("\n") + ending
    )
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "opus")
    assert run(tmp_path, "check").returncode == 0


# --- signals, and what a killed run leaves behind -----------------------------

# Runs the tool as a script and sends the process a signal right after its
# first rename has landed, which is the moment a partial write would show.
SIGNAL_HARNESS = """\
import os
import runpy
import signal
import sys

signum = int(sys.argv[1])
script = sys.argv[2]
# A test runner started in the background, or under nohup, hands its children
# these signals ignored. Start from the dispositions an interactive run has.
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)
real_replace = os.replace
calls = {"count": 0}


def replace(source, destination):
    calls["count"] += 1
    real_replace(source, destination)
    if calls["count"] == 1:
        os.kill(os.getpid(), signum)


os.replace = replace
sys.argv = [script, *sys.argv[3:]]
runpy.run_path(script, run_name="__main__")
"""


@pytest.mark.parametrize("interpreter", INTERPRETERS, ids=lambda path: Path(path).name)
@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_signal_restores_the_files_and_ends_the_run_with_that_signal(
    signal_name: str, interpreter: str, tmp_path: Path
) -> None:
    """Ctrl-C, a cancelled job, a closed terminal: the files go back as they
    were, no temporary file stays, and the process dies of the signal so that
    the script that started it stops too instead of carrying on."""
    signum = int(getattr(signal, signal_name))
    project = make_project(tmp_path / "project")
    write(project, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(project, ".claude/agents/beta.md", agent_text("beta", "opus"))
    harness = tmp_path / "harness.py"
    harness.write_text(SIGNAL_HARNESS, encoding="utf-8")
    before = snapshot(project)

    result = subprocess.run(
        [
            interpreter,
            str(harness),
            str(signum),
            str(SCRIPT),
            "--project-root",
            str(project),
            "apply",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == -signum, (result.returncode, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["result"] == "interrupted"
    assert "every file was restored" in payload["error"]
    assert "Traceback" not in result.stderr
    assert snapshot(project) == before


def test_apply_removes_what_a_killed_run_left_behind(
    tmp_path: Path, run: Runner
) -> None:
    """A run killed outright cannot clean up. Its temporary files are full,
    untracked copies of the files beside them, one `git add -A` away from a
    commit, so the gate reports them and the next apply removes them."""
    make_project(tmp_path)
    pristine = snapshot(tmp_path)
    orphans = [
        ".claude/.settings.json.models-tmp.4242",
        ".claude/agents/.alpha.md.models-tmp.4242",
        ".claude/skills/_shared/.models.json.models-tmp.4242",
        ".codex/.config.toml.models-tmp.4242",
    ]
    for rel in orphans:
        write(tmp_path, rel, "a stale copy\n")

    check = run(tmp_path, "check")

    assert check.returncode == 2, check.stdout
    assert sorted(payload_of(check)["orphans"]) == orphans
    assert check.stderr.count("orphan: ") == len(orphans)

    applied = run(tmp_path, "apply")

    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert sorted(payload_of(applied)["cleaned"]) == orphans
    assert snapshot(tmp_path) == pristine
    assert run(tmp_path, "check").returncode == 0


def test_a_fifo_where_the_override_belongs_is_refused_not_opened(
    tmp_path: Path, run: Runner
) -> None:
    """Opening a FIFO blocks until someone writes to it: the tool, the gate
    and every Codex call would hang."""
    make_project(tmp_path)
    os.mkfifo(tmp_path / LOCAL_REL)

    result = run(tmp_path, "check")

    assert result.returncode == 1, result.stdout
    assert "not a regular file" in payload_of(result)["error"]


# --- failures while reading, and a rollback that fails -------------------------


def test_an_unreadable_settings_file_is_refused(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The denial is injected rather than staged with `chmod`, so the case
    holds for a user that file modes do not restrict."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    before = snapshot(tmp_path)
    real_read_bytes = Path.read_bytes

    def denied(self: Path) -> bytes:
        if self.name == "settings.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", denied)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    monkeypatch.undo()
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_REFUSED
    assert payload["refused"][0]["file"] == SETTINGS_REL
    assert "cannot be read" in payload["refused"][0]["reason"]
    assert snapshot(tmp_path) == before


def test_an_unlistable_agents_directory_is_an_error_not_a_traceback(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It must not read as "this project has no agents", which would be a
    refusal a caller continues past."""
    make_project(tmp_path)
    real_listdir = os.listdir

    def denied(path: Any = ".") -> list[str]:
        if str(path).endswith("agents"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(models_module.os, "listdir", denied)

    exit_code = models_module.main(["--project-root", str(tmp_path), "check"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_FAILED
    assert json.loads(captured.out)["ok"] is False
    assert captured.err.startswith("error: ")
    assert "Permission denied" in captured.err


def test_a_rollback_that_fails_is_reported_as_not_undone(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one message that says a project was left half-written."""
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    real_replace = os.replace
    calls = {"count": 0}

    def replace_once(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("simulated dead filesystem")
        real_replace(source, destination)

    monkeypatch.setattr(models_module.os, "replace", replace_once)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == EXIT_FAILED
    assert payload["result"] == "failed"
    assert "could not be undone" in payload["error"]
    assert "alpha.md" in payload["error"]
    assert "every file was restored" not in payload["error"]
    assert captured.err.startswith("error: write failed")
    # Reported truthfully: the first file did stay rewritten.
    assert read(tmp_path, ".claude/agents/alpha.md") == agent_text("alpha", "opus")
    assert not [name for name in snapshot(tmp_path) if "models-tmp" in name]


def test_a_rollback_leaves_a_file_someone_saved_again(
    tmp_path: Path,
    models_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_project(tmp_path)
    write(tmp_path, ".claude/agents/alpha.md", agent_text("alpha", "sonnet"))
    write(tmp_path, ".claude/agents/beta.md", agent_text("beta", "opus"))
    saved_meanwhile = agent_text("alpha", "haiku")
    real_replace = os.replace
    calls = {"count": 0}

    def fail_after_a_save(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            write(tmp_path, ".claude/agents/alpha.md", saved_meanwhile)
            raise OSError("simulated rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(models_module.os, "replace", fail_after_a_save)

    exit_code = models_module.main(["--project-root", str(tmp_path), "apply"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_FAILED
    assert "was changed again and is left as it is" in payload["error"]
    assert read(tmp_path, ".claude/agents/alpha.md") == saved_meanwhile
    assert read(tmp_path, ".claude/agents/beta.md") == agent_text("beta", "opus")


@pytest.mark.parametrize("interpreter", INTERPRETERS, ids=lambda path: Path(path).name)
def test_invalid_toml_below_the_top_level_is_caught_only_where_a_parser_exists(
    interpreter: str, tmp_path: Path
) -> None:
    """The one place the two interpreter generations differ, pinned so it
    cannot drift unnoticed. Where `tomllib` exists the edit is checked with
    it and an invalid file is refused. Without it only the top-level region
    is examined, so a defect inside a later table goes unseen and the managed
    line is still edited correctly."""
    make_project(tmp_path)
    broken_table = "\n[broken]\nvalue = \n"
    stale = FIXTURE_CONFIG.replace('"gpt-9.1-test"', '"gpt-8-old"') + broken_table
    write(tmp_path, CONFIG_REL, stale)

    result = subprocess.run(
        [interpreter, str(SCRIPT), "--project-root", str(tmp_path), "apply"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    version = _version_of(Path(interpreter))
    assert version is not None
    if version >= FIRST_WITH_TOMLLIB:
        assert result.returncode == EXIT_REFUSED, result.stdout
        assert "not valid TOML" in payload_of(result)["refused"][0]["reason"]
        assert read(tmp_path, CONFIG_REL) == stale
    else:
        assert result.returncode == 0, result.stdout
        assert read(tmp_path, CONFIG_REL) == FIXTURE_CONFIG + broken_table


# --- this repository ----------------------------------------------------------


def test_the_template_matches_its_own_manifest(run: Runner) -> None:
    result = run(REPO_ROOT, "check", "--strict")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = payload_of(result)
    assert payload["result"] == "in-sync"
    # In a project a never-shipped value is a deliberate choice and passes the
    # gate. The template has no such choices: here it would be a typo that
    # `check` alone reports as `custom`, so it is pinned to empty.
    assert payload["custom"] == []
    assert payload["skipped"] == []
    assert payload["refused"] == []
    assert payload["strict"] is True
    assert payload["strict_findings"] == []
    assert payload["orphans"] == []


def test_the_committed_manifest_is_in_the_layout_set_writes() -> None:
    """Otherwise the first ``set`` would reformat the file, and a model bump
    would no longer be a diff a reviewer can read at a glance."""
    text = (REPO_ROOT / MANIFEST_REL).read_text(encoding="utf-8")

    assert text == canonical(json.loads(text))


def test_the_template_ships_no_local_override() -> None:
    assert not (REPO_ROOT / LOCAL_REL).exists()


def test_the_tool_stays_importable_on_the_oldest_supported_python() -> None:
    """``scripts/update.sh`` runs it with whatever ``python3`` a project has.

    The syntax must parse as Python 3.9, annotations must stay unevaluated (so
    ``X | None`` is legal there), and ``tomllib`` (3.11+) may only be imported
    where an ImportError is handled.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=OLDEST_SUPPORTED)

    future = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    assert any(alias.name == "annotations" for node in future for alias in node.names)

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handled = [
            name.id
            for handler in node.handlers
            for name in ast.walk(handler.type or ast.Tuple(elts=[]))
            if isinstance(name, ast.Name)
        ]
        if "ImportError" in handled or "ModuleNotFoundError" in handled:
            guarded.update(id(child) for body in node.body for child in ast.walk(body))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if "tomllib" in modules:
            assert id(node) in guarded, "tomllib is imported without a fallback"

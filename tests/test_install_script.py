from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"
SETTINGS_REL = ".claude/settings.json"
CONFIG_REL = ".codex/config.toml"
MODELS_LOCAL_REL = ".claude/models.local.json"
# Stand-ins for values a project chose itself. No template version shipped
# them, so the model manifest must never replace them.
CUSTOM_CODEX_MODEL = "gpt-project-own-pin"
CUSTOM_CLAUDE_MODEL = "claude-project-9-pin"
CUSTOM_EFFORT = "low"


def init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def run_install(
    target: Path,
    *options: str,
    script: Path = INSTALL_SCRIPT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--yes", *options, str(target)],
        check=False,
        capture_output=True,
        text=True,
    )


def copy_local_excludes(template: Path) -> None:
    """Carry this checkout's `.git/info/exclude` over to the scratch template.

    The copy below takes the whole working directory, including files a
    developer keeps here but excludes locally (notes, plans). They are not
    template content, and the real repository would never commit them; without
    the same excludes the scratch one would, and the gates would judge them.
    """
    located = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-path", "info/exclude"],
        check=False,
        capture_output=True,
        text=True,
    )
    source = REPO_ROOT / located.stdout.strip()
    if located.returncode != 0 or not source.is_file():
        return
    target = template / ".git" / "info" / "exclude"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def build_template_repo(tmp_path: Path, name: str = "template") -> Path:
    """Copy this repository into a fresh git repo so scripts under test can
    be run against a template source distinct from the live checkout."""
    template = tmp_path / name
    shutil.copytree(
        REPO_ROOT,
        template,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            # Live agent worktrees are full copies of the repository.
            "worktrees",
        ),
    )
    subprocess.run(["git", "init", "-q", str(template)], check=True)
    copy_local_excludes(template)
    subprocess.run(
        ["git", "-C", str(template), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(template), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "template"], check=True
    )
    return template


def run_update(
    target: Path,
    template: Path,
    *options: str,
    extra_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ORCHESTRA_TEMPLATE_REPO": str(template)}
    if extra_path is not None:
        env["PATH"] = f"{extra_path}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(target / "scripts/update.sh"), "--yes", *options],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def install_from_template(tmp_path: Path) -> tuple[Path, Path]:
    """Build a template repo and install it into a fresh project."""
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    result = run_install(target, script=template / "scripts/install.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    return template, target


def canonical_json(value: Any) -> str:
    """The layout the template ships its settings in, and the tool rewrites."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def read_settings(target: Path) -> dict[str, Any]:
    return json.loads((target / SETTINGS_REL).read_text(encoding="utf-8"))


def set_toml_value(text: str, key: str, value: str) -> str:
    """Replace one top-level ``key = "..."`` line, whatever it holds today."""
    edited, count = re.subn(
        rf'(?m)^{re.escape(key)} = "[^"]*"$', f'{key} = "{value}"', text
    )
    assert count == 1, f"{key} is not a single plain top-level line"
    return edited


def never_shipped(resolved: dict[str, Any], key: str, candidates: list[str]) -> str:
    """Pick a value for *key* that no template version ever shipped."""
    section, _, leaf = key.partition(".")
    shipped = {*resolved["superseded"].get(key, []), resolved[section][leaf]}
    return next(value for value in candidates if value not in shipped)


def write_python3_shim(shim_dir: Path, on_apply: str) -> None:
    """Put a `python3` first on PATH that runs *on_apply* (a shell command)
    just before the model tool's `apply`, and is the real interpreter for
    everything else. It stages a condition at exactly that point of an update
    without touching the scripts or the tool under test."""
    real = shutil.which("python3")
    assert real is not None
    shim_dir.mkdir()
    shim = shim_dir / "python3"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '    if [[ "${arg}" == "apply" ]]; then\n'
        f"        {on_apply}\n"
        "    fi\n"
        "done\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


# Runs the model tool with every attempt to create a file under `.codex/`
# denied, which is what a read-only directory does. Denying it inside the
# interpreter instead of with `chmod` makes it hold for any user, and at
# exactly the point where the tool writes.
DENY_CODEX_WRITES = """\
import os
import runpy
import sys

real_open = os.open


def deny(path, flags, mode=0o777, **kwargs):
    if os.sep + ".codex" + os.sep in str(path) and flags & os.O_CREAT:
        raise PermissionError(13, "Permission denied", str(path))
    return real_open(path, flags, mode, **kwargs)


os.open = deny
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def bump_template_version(template: Path, version: str) -> None:
    """Give the template a new VERSION, so a test can tell whether the update
    reached the phase that records it."""
    (template / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(template), "commit", "-qm", "bump"], check=True)


def run_gate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )


def find_debris(root: Path) -> list[Path]:
    """Locate any leftover stage-and-swap artifacts under root (excluding .git)."""
    debris: list[Path] = []
    for pattern in ("*.orchestra-staging.*", "*.orchestra-old.*"):
        for match in root.rglob(pattern):
            if ".git" in match.relative_to(root).parts:
                continue
            debris.append(match)
    return debris


def test_install_adds_complete_template_without_overwriting_project_version(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "README.md").write_text("# Existing project\n", encoding="utf-8")
    (target / "VERSION").write_text("9.4.1\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert (target / "README.md").read_text(encoding="utf-8") == "# Existing project\n"
    assert (target / "VERSION").read_text(encoding="utf-8") == "9.4.1\n"
    assert (target / ".claude/orchestra-version").read_text(encoding="utf-8") == (
        REPO_ROOT / "VERSION"
    ).read_text(encoding="utf-8")
    assert (target / ".claude/docs/INDEX.md").is_file()
    assert (target / ".claude/docs/change_main.md").is_file()
    assert (target / ".agents/AGENTS.md").is_file()
    assert (target / ".claude/rules/tiers.md").is_file()
    assert (target / ".codex/AGENTS.md").is_file()
    assert "## Skill Catalog" in (target / "AGENTS.md").read_text(encoding="utf-8")
    assert not (target / ".claude/rules/orchestration.md").exists()
    assert (target / ".claude/rules").is_dir()
    assert (target / ".claude/skills").is_dir()
    assert (target / ".claude/agents").is_dir()
    assert (target / ".claude/hooks").is_dir()
    assert (target / "AGENTS.md").is_file()
    assert not (target / "AGENTS.md").is_symlink()
    # Claude Code reads AGENTS.md only when no CLAUDE.md shadows it, so the
    # installer must never create one.
    assert not (target / "CLAUDE.md").exists()
    assert not (target / "CLAUDE.md").is_symlink()
    assert not (target / ".claude/CLAUDE.md").exists()
    assert {path.name for path in (target / ".claude").iterdir()} == {
        "orchestra-version",
        "settings.json",
        "STATE.md",
        "agents",
        "skills",
        "rules",
        "hooks",
        "docs",
        "logs",
        "checkpoints",
    }
    assert not any(path.is_symlink() for path in (target / ".claude").iterdir())
    assert {path.name for path in (target / ".codex").iterdir()} == {
        "config.toml",
        "AGENTS.md",
    }
    assert (target / "scripts/install.sh").is_file()
    assert (target / "scripts/update.sh").is_file()
    assert (target / ".claude/settings.json").is_file()
    assert (target / ".claude/docs/research/.gitkeep").is_file()
    assert (target / ".claude/STATE.md").is_file()
    assert "@orchestra:" not in (target / "AGENTS.md").read_text(encoding="utf-8")
    gitignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/logs/" in gitignore
    assert ".claude/checkpoints/" in gitignore
    assert ".orchestra-backup-*/" in gitignore


def test_install_preserves_existing_claude_md_in_shared_state(tmp_path: Path) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    existing_content = "# Existing instructions\n\nKeep this project rule.\n"
    (target / "CLAUDE.md").write_text(existing_content, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    installed = (target / ".claude/STATE.md").read_text(encoding="utf-8")
    assert installed.count(existing_content.strip()) == 1
    assert existing_content.strip() not in (target / "AGENTS.md").read_text(
        encoding="utf-8"
    )


def test_install_moves_an_existing_claude_md_aside_with_a_warning(
    tmp_path: Path,
) -> None:
    """A CLAUDE.md left in place would load instead of AGENTS.md, so it is
    renamed — never deleted — and the user is told why."""
    target = tmp_path / "project"
    init_git_repo(target)
    existing_content = "# Existing instructions\n\nKeep this project rule.\n"
    (target / "CLAUDE.md").write_text(existing_content, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert not (target / "CLAUDE.md").exists()
    backup = target / "CLAUDE.md.pre-agents-md.bak"
    assert backup.read_text(encoding="utf-8") == existing_content
    output = result.stdout + result.stderr
    assert "CLAUDE.md.pre-agents-md.bak" in output
    assert "instead of AGENTS.md" in output


def test_install_keeps_an_earlier_claude_md_backup(tmp_path: Path) -> None:
    """Re-running the installer must not overwrite a backup it made before."""
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "CLAUDE.md.pre-agents-md.bak").write_text("first\n", encoding="utf-8")
    (target / "CLAUDE.md").write_text("second\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert (target / "CLAUDE.md.pre-agents-md.bak").read_text(
        encoding="utf-8"
    ) == "first\n"
    extra = [
        path
        for path in target.glob("CLAUDE.md.pre-agents-md.bak.*")
        if path.read_text(encoding="utf-8") == "second\n"
    ]
    assert len(extra) == 1


def test_install_refuses_template_owned_path_conflicts_by_default(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    custom_file = target / ".claude/rules/custom.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("custom contract\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 2
    assert "--force" in result.stderr
    assert custom_file.read_text(encoding="utf-8") == "custom contract\n"
    assert not (target / "AGENTS.md").exists()


def test_force_install_backs_up_conflicts_before_replacing_them(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    custom_file = target / ".claude/rules/custom.md"
    custom_file.parent.mkdir(parents=True)
    custom_file.write_text("custom contract\n", encoding="utf-8")

    result = run_install(target, "--force")

    assert result.returncode == 0, result.stderr
    backups = list(target.glob(".orchestra-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / ".claude/rules/custom.md").read_text(
        encoding="utf-8"
    ) == "custom contract\n"
    assert (target / ".claude/docs/INDEX.md").is_file()
    assert not (target / ".claude/rules/custom.md").exists()


def test_install_refuses_to_destroy_existing_native_subagents_and_skills(
    tmp_path: Path,
) -> None:
    """A repo that already uses Claude Code natively keeps its own subagents and
    skills in .claude/. Linking those paths must never silently delete them."""
    target = tmp_path / "project"
    init_git_repo(target)
    existing_agent = target / ".claude/agents/my-agent.md"
    existing_agent.parent.mkdir(parents=True)
    existing_agent.write_text("user's own subagent\n", encoding="utf-8")
    existing_skill = target / ".claude/skills/my-skill/SKILL.md"
    existing_skill.parent.mkdir(parents=True)
    existing_skill.write_text("user's own skill\n", encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 2
    assert "--force" in result.stderr
    assert existing_agent.read_text(encoding="utf-8") == "user's own subagent\n"
    assert existing_skill.read_text(encoding="utf-8") == "user's own skill\n"
    assert not (target / "AGENTS.md").exists()


def test_force_install_backs_up_existing_native_subagents_and_skills(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    existing_agent = target / ".claude/agents/my-agent.md"
    existing_agent.parent.mkdir(parents=True)
    existing_agent.write_text("user's own subagent\n", encoding="utf-8")

    result = run_install(target, "--force")

    assert result.returncode == 0, result.stderr
    backups = list(target.glob(".orchestra-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / ".claude/agents/my-agent.md").read_text(
        encoding="utf-8"
    ) == "user's own subagent\n"
    assert (target / ".claude/agents").is_dir()
    assert not (target / ".claude/agents").is_symlink()
    assert (target / ".claude/agents/general-purpose-deep.md").is_file()


def test_install_retires_a_legacy_claude_md_symlink(tmp_path: Path) -> None:
    """Pre-2.0 installs shipped CLAUDE.md as a symlink to AGENTS.md. AGENTS.md
    is the only root contract now, so the link is reported as a conflict and
    only removed once the user accepts the backup."""
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "AGENTS.md").write_text("# Old bootstrap\n", encoding="utf-8")
    (target / "CLAUDE.md").symlink_to("AGENTS.md")

    refused = run_install(target)
    assert refused.returncode == 2
    assert "CLAUDE.md" in refused.stdout + refused.stderr

    result = run_install(target, "--force")

    assert result.returncode == 0, result.stderr
    assert not (target / "CLAUDE.md").exists()
    assert not (target / "CLAUDE.md").is_symlink()
    assert "## Skill Catalog" in (target / "AGENTS.md").read_text(encoding="utf-8")
    backups = list(target.glob(".orchestra-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "CLAUDE.md").is_symlink()


def test_install_preserves_existing_settings_and_writes_merge_candidate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    settings = target / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    custom_settings = '{"language": "english"}\n'
    settings.write_text(custom_settings, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert settings.read_text(encoding="utf-8") == custom_settings
    candidate = target / ".claude/settings.orchestra.json"
    assert candidate.read_text(encoding="utf-8") == (
        REPO_ROOT / ".claude/settings.json"
    ).read_text(encoding="utf-8")
    assert "settings.orchestra.json" in result.stdout


def test_install_preserves_existing_codex_config_and_writes_merge_candidate(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """`.codex/config.toml` is a settings file, not template content.

    It used to sit inside the wholesale-replaced `.codex/` directory, so an
    install or update silently overwrote a downstream project's model choice,
    approval policy, and sandbox mode — while `.claude/settings.json`, the same
    kind of file, was carefully preserved.

    The model manifest patches single keys in it, and only ones the project
    did not choose: the model the project pinned stays, and the one managed
    key the file lacked is added.
    """
    target = tmp_path / "project"
    init_git_repo(target)
    config = target / CONFIG_REL
    config.parent.mkdir(parents=True)
    custom_config = f'model = "{CUSTOM_CODEX_MODEL}"\napproval_policy = "on-request"\n'
    config.write_text(custom_config, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    effort = resolved_models["codex"]["reasoning_effort"]
    assert config.read_text(encoding="utf-8") == (
        custom_config + f'model_reasoning_effort = "{effort}"\n'
    )
    assert f'kept "{CUSTOM_CODEX_MODEL}"' in result.stdout
    candidate = target / ".codex/config.orchestra.toml"
    assert candidate.read_text(encoding="utf-8") == (
        REPO_ROOT / ".codex/config.toml"
    ).read_text(encoding="utf-8")
    assert "config.orchestra.toml" in result.stdout
    # The adapter beside it is template content and is still installed.
    assert (target / ".codex/AGENTS.md").is_file()


def test_install_reports_a_settings_layout_it_will_not_edit(tmp_path: Path) -> None:
    """A one-line settings file is valid JSON this tool cannot rewrite without
    reformatting it, so it is left alone and the installer says so."""
    target = tmp_path / "project"
    init_git_repo(target)
    settings = target / SETTINGS_REL
    settings.parent.mkdir(parents=True)
    custom_settings = '{"language": "english"}\n'
    settings.write_text(custom_settings, encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    assert settings.read_text(encoding="utf-8") == custom_settings
    assert f"refused: {SETTINGS_REL}" in result.stdout
    assert "Model manifest was NOT applied" in result.stdout


def test_install_advances_a_shipped_default_in_existing_settings(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    settings = target / SETTINGS_REL
    settings.parent.mkdir(parents=True)
    shipped = resolved_models["superseded"]["claude.subagent_default"][0]
    existing = {
        "language": "english",
        "env": {"CLAUDE_CODE_SUBAGENT_MODEL": shipped, "KEEP_ME": "1"},
    }
    settings.write_text(canonical_json(existing), encoding="utf-8")

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    expected = {
        "language": "english",
        "env": {
            "CLAUDE_CODE_SUBAGENT_MODEL": resolved_models["claude"]["subagent_default"],
            "KEEP_ME": "1",
        },
        "model": resolved_models["claude"]["main"],
    }
    assert settings.read_text(encoding="utf-8") == canonical_json(expected)


def test_install_finishes_then_fails_loudly_on_an_invalid_model_override(
    tmp_path: Path,
) -> None:
    """A project that already carries a broken override still gets a complete
    installation, and a non-zero exit so the unapplied models are not missed."""
    target = tmp_path / "project"
    init_git_repo(target)
    override = target / MODELS_LOCAL_REL
    override.parent.mkdir(parents=True)
    override.write_text('{"schema_version": 1, "codex": {"modle": "typo"}}')

    result = run_install(target)

    assert result.returncode == 1, result.stdout
    assert "models.local.json" in result.stderr
    assert "model manifest was not applied" in result.stderr
    # Everything after the model step still ran.
    assert (target / ".claude/orchestra-version").is_file()
    assert ".claude/logs/" in (target / ".gitignore").read_text(encoding="utf-8")


def test_update_keeps_custom_model_values_and_reports_them(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """A value the project chose survives an update byte for byte.

    This used to hold trivially, because the updater never wrote to either
    settings file. It now patches the manifest's model keys, so what protects
    a deliberate choice is the rule that only a value some template version
    shipped may be replaced.
    """
    template, target = install_from_template(tmp_path)
    config = target / CONFIG_REL
    custom_effort = never_shipped(
        resolved_models, "codex.reasoning_effort", [CUSTOM_EFFORT, "max"]
    )
    edited_config = set_toml_value(
        set_toml_value(config.read_text(encoding="utf-8"), "model", CUSTOM_CODEX_MODEL),
        "model_reasoning_effort",
        custom_effort,
    )
    config.write_text(edited_config, encoding="utf-8")
    settings = read_settings(target)
    settings["model"] = CUSTOM_CLAUDE_MODEL
    settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = CUSTOM_CLAUDE_MODEL
    settings["env"]["CODEX_MODEL"] = CUSTOM_CODEX_MODEL
    edited_settings = canonical_json(settings)
    (target / SETTINGS_REL).write_text(edited_settings, encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert config.read_text(encoding="utf-8") == edited_config
    assert (target / SETTINGS_REL).read_text(encoding="utf-8") == edited_settings
    for kept in (
        f'model: kept "{CUSTOM_CLAUDE_MODEL}"',
        f'env.CLAUDE_CODE_SUBAGENT_MODEL: kept "{CUSTOM_CLAUDE_MODEL}"',
        f'env.CODEX_MODEL: kept "{CUSTOM_CODEX_MODEL}"',
        f'{CONFIG_REL} model: kept "{CUSTOM_CODEX_MODEL}"',
        f'model_reasoning_effort: kept "{custom_effort}"',
    ):
        assert kept in update_result.stdout, kept
    # A deliberate value is not drift, so the project's own gate stays green.
    check = subprocess.run(
        ["bash", str(target / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_update_advances_shipped_model_defaults_and_nothing_else(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """The gap this closes: a template model bump never reached an installed
    project, because both settings files were only ever shown as a diff."""
    template, target = install_from_template(tmp_path)
    superseded = resolved_models["superseded"]
    config = target / CONFIG_REL
    pristine_config = config.read_text(encoding="utf-8")
    project_config = pristine_config.replace(
        'approval_policy = "never"', 'approval_policy = "on-request"'
    )
    assert project_config != pristine_config
    stale_config = set_toml_value(
        set_toml_value(project_config, "model", superseded["codex.model"][0]),
        "model_reasoning_effort",
        superseded["codex.reasoning_effort"][0],
    )
    config.write_text(stale_config, encoding="utf-8")

    project_settings = read_settings(target)
    project_settings["language"] = "english"
    stale_settings = json.loads(json.dumps(project_settings))
    del stale_settings["model"]
    stale_settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = superseded[
        "claude.subagent_default"
    ][0]
    stale_settings["env"]["CODEX_MODEL"] = superseded["codex.model"][0]
    (target / SETTINGS_REL).write_text(canonical_json(stale_settings), encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    # Only the managed values moved: the project's own edits are byte-identical.
    assert config.read_text(encoding="utf-8") == project_config
    updated = read_settings(target)
    assert updated == project_settings
    assert "CODEX_MODEL" not in updated["env"]
    assert updated["model"] == resolved_models["claude"]["main"]
    assert f'{CONFIG_REL} model: "{superseded["codex.model"][0]}"' in (
        update_result.stdout
    )
    assert "custom:" not in update_result.stdout
    # A second run has nothing left to do.
    before = (config.read_bytes(), (target / SETTINGS_REL).read_bytes())
    assert run_update(target, template).returncode == 0
    assert (config.read_bytes(), (target / SETTINGS_REL).read_bytes()) == before


def test_update_honours_the_project_model_override(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """Agent definitions are replaced wholesale on every update, so a project
    override only survives because the update re-renders them afterwards."""
    template, target = install_from_template(tmp_path)
    agent = sorted(resolved_models["claude"]["agents"])[0]
    assigned = resolved_models["claude"]["agents"][agent]
    override_model = next(m for m in ("haiku", "sonnet") if m != assigned)
    override = {
        "schema_version": 1,
        "claude": {"agents": {agent: override_model}},
        "codex": {"model": CUSTOM_CODEX_MODEL},
    }
    override_text = json.dumps(override, indent=2) + "\n"
    (target / MODELS_LOCAL_REL).write_text(override_text, encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    agent_file = (target / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8")
    assert f"\nmodel: {override_model}\n" in agent_file
    assert f'model = "{CUSTOM_CODEX_MODEL}"' in (target / CONFIG_REL).read_text(
        encoding="utf-8"
    )
    # The override is the project's file: never shipped, never rewritten.
    assert (target / MODELS_LOCAL_REL).read_text(encoding="utf-8") == override_text
    assert not (template / MODELS_LOCAL_REL).exists()
    check = subprocess.run(
        ["bash", str(target / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_update_leaves_native_settings_alone_when_the_project_opts_out(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    template, target = install_from_template(tmp_path)
    superseded = resolved_models["superseded"]
    config = target / CONFIG_REL
    stale_config = set_toml_value(
        config.read_text(encoding="utf-8"),
        "model_reasoning_effort",
        superseded["codex.reasoning_effort"][0],
    )
    config.write_text(stale_config, encoding="utf-8")
    settings = read_settings(target)
    settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = superseded[
        "claude.subagent_default"
    ][0]
    stale_settings = canonical_json(settings)
    (target / SETTINGS_REL).write_text(stale_settings, encoding="utf-8")
    (target / MODELS_LOCAL_REL).write_text(
        json.dumps({"schema_version": 1, "manage_native_settings": False}),
        encoding="utf-8",
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert config.read_text(encoding="utf-8") == stale_config
    assert (target / SETTINGS_REL).read_text(encoding="utf-8") == stale_settings


REFUSED_CONFIG_LAYOUTS = {
    "single-quoted value": lambda text: text.replace(
        "\nmodel_reasoning_effort = ", "\nmodel_reasoning_effort = 'x' # ", 1
    ),
    "multi-line string": lambda text: 'notes = """\nmodel = "inside a string"\n"""\n'
    + text,
    "duplicate key": lambda text: text.replace(
        "\nmodel_reasoning_effort = ",
        '\nmodel_reasoning_effort = "medium"\nmodel_reasoning_effort = ',
        1,
    ),
}


@pytest.mark.parametrize("layout", sorted(REFUSED_CONFIG_LAYOUTS))
def test_update_refuses_an_unusual_config_layout_and_changes_no_file(
    layout: str, tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """One file the tool will not edit means no file is edited: a project is
    never left with its settings advanced and its Codex config not."""
    template, target = install_from_template(tmp_path)
    config = target / CONFIG_REL
    pristine = config.read_text(encoding="utf-8")
    odd_config = REFUSED_CONFIG_LAYOUTS[layout](pristine)
    assert odd_config != pristine
    config.write_text(odd_config, encoding="utf-8")
    # The settings file is in a layout the tool can edit and needs an edit.
    settings = read_settings(target)
    settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = resolved_models["superseded"][
        "claude.subagent_default"
    ][0]
    stale_settings = canonical_json(settings)
    (target / SETTINGS_REL).write_text(stale_settings, encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert config.read_text(encoding="utf-8") == odd_config
    assert (target / SETTINGS_REL).read_text(encoding="utf-8") == stale_settings
    assert f"refused: {CONFIG_REL}" in update_result.stdout
    assert "Model manifest was NOT applied" in update_result.stdout
    # The update itself still ran to completion.
    assert "Done!" in update_result.stdout
    assert find_debris(target) == []


def test_update_refusal_says_that_agent_overrides_are_not_in_effect(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """Agent definitions are replaced from the template before the model step.
    When that step then refuses a file, nothing re-renders them, so a project
    override silently stops applying unless the update says so."""
    template, target = install_from_template(tmp_path)
    agent = sorted(resolved_models["claude"]["agents"])[0]
    assigned = resolved_models["claude"]["agents"][agent]
    override_model = next(m for m in ("haiku", "sonnet") if m != assigned)
    (target / MODELS_LOCAL_REL).write_text(
        json.dumps(
            {"schema_version": 1, "claude": {"agents": {agent: override_model}}}
        ),
        encoding="utf-8",
    )
    config = target / CONFIG_REL
    odd_config = REFUSED_CONFIG_LAYOUTS["single-quoted value"](
        config.read_text(encoding="utf-8")
    )
    config.write_text(odd_config, encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    agent_file = (target / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8")
    assert f"\nmodel: {assigned}\n" in agent_file
    assert config.read_text(encoding="utf-8") == odd_config
    assert "models.local.json is not in effect" in update_result.stdout
    # Once the reported line is fixed, a plain apply finishes the job.
    config.write_text(
        (template / CONFIG_REL).read_text(encoding="utf-8"), encoding="utf-8"
    )
    applied = subprocess.run(
        ["python3", str(target / ".claude/skills/_shared/models.py"), "apply"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    agent_file = (target / ".claude/agents" / f"{agent}.md").read_text(encoding="utf-8")
    assert f"\nmodel: {override_model}\n" in agent_file


def test_update_finishes_then_fails_loudly_on_an_invalid_model_override(
    tmp_path: Path,
) -> None:
    """An unreadable override is not a layout to skip past: the update still
    completes every phase, so the project is not left half-updated, and then
    exits non-zero so the failure cannot be missed."""
    template, target = install_from_template(tmp_path)
    (template / "VERSION").write_text("99.0.0\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(template), "commit", "-qm", "bump"], check=True)
    (target / MODELS_LOCAL_REL).write_text(
        json.dumps({"schema_version": 1, "codex": {"modle": "typo"}}), encoding="utf-8"
    )
    settings_before = (target / SETTINGS_REL).read_bytes()
    config_before = (target / CONFIG_REL).read_bytes()

    update_result = run_update(target, template)

    assert update_result.returncode == 1
    combined = update_result.stdout + update_result.stderr
    assert "models.local.json" in combined
    assert "model manifest was not applied" in update_result.stderr
    assert (target / ".claude/orchestra-version").read_text(encoding="utf-8") == (
        "99.0.0\n"
    )
    assert (target / SETTINGS_REL).read_bytes() == settings_before
    assert (target / CONFIG_REL).read_bytes() == config_before
    assert find_debris(target) == []


def test_update_fails_loudly_when_the_model_write_fails(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """A failed write is not a refused layout. The tool reports the two with
    different exit codes, and the update must act on that difference: here it
    finishes its remaining phases and exits non-zero, and it must not claim
    that the models were merely "not applied"."""
    template, target = install_from_template(tmp_path)
    bump_template_version(template, "99.0.0")
    config = target / CONFIG_REL
    stale_config = set_toml_value(
        config.read_text(encoding="utf-8"),
        "model_reasoning_effort",
        resolved_models["superseded"]["codex.reasoning_effort"][0],
    )
    config.write_text(stale_config, encoding="utf-8")
    settings_before = (target / SETTINGS_REL).read_bytes()
    # `.codex/` is not writable for the tool, so the temporary file for
    # config.toml cannot be created; the one for settings.json already has been.
    wrapper = tmp_path / "deny_codex_writes.py"
    wrapper.write_text(DENY_CODEX_WRITES, encoding="utf-8")
    shim_dir = tmp_path / "shim"
    write_python3_shim(shim_dir, f'exec "{shutil.which("python3")}" "{wrapper}" "$@"')

    update_result = run_update(target, template, extra_path=shim_dir)

    assert update_result.returncode == 1, update_result.stdout
    assert "could not be applied" in update_result.stderr
    assert "NOT applied; no file was changed" not in update_result.stdout
    assert "Model manifest applied" not in update_result.stdout
    # The phases after the model step still ran.
    assert (target / ".claude/orchestra-version").read_text(encoding="utf-8") == (
        "99.0.0\n"
    )
    assert "Update Summary" in update_result.stdout
    # All-or-nothing held: nothing was written and nothing was left behind.
    assert config.read_text(encoding="utf-8") == stale_config
    assert (target / SETTINGS_REL).read_bytes() == settings_before
    assert not list(target.rglob("*models-tmp*"))
    assert find_debris(target) == []


def test_install_fails_loudly_when_the_model_write_fails(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """The installer draws the same line as the updater between a refused
    layout and a failed write, from the tool's exit code alone."""
    target = tmp_path / "project"
    init_git_repo(target)
    config = target / CONFIG_REL
    config.parent.mkdir(parents=True)
    # A config the project already had, still on a default an earlier
    # template shipped: the model step has to write to `.codex/`.
    stale_config = (
        f'model_reasoning_effort = "'
        f'{resolved_models["superseded"]["codex.reasoning_effort"][0]}"\n'
    )
    config.write_text(stale_config, encoding="utf-8")
    wrapper = tmp_path / "deny_codex_writes.py"
    wrapper.write_text(DENY_CODEX_WRITES, encoding="utf-8")
    shim_dir = tmp_path / "shim"
    write_python3_shim(shim_dir, f'exec "{shutil.which("python3")}" "{wrapper}" "$@"')

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--yes", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 1, result.stdout
    assert "could not be applied" in result.stderr
    assert "NOT applied; no file was changed" not in result.stdout
    # The steps after the model step still ran.
    assert (target / ".claude/orchestra-version").is_file()
    assert "installation complete" in result.stdout
    assert config.read_text(encoding="utf-8") == stale_config
    assert not list(target.rglob("*models-tmp*"))


def test_update_stops_when_the_model_step_is_interrupted(tmp_path: Path) -> None:
    """Ctrl-C during the model step ends the tool with the signal. The update
    must stop there, not report a failure and carry on with later phases."""
    template, target = install_from_template(tmp_path)
    bump_template_version(template, "99.0.0")
    installed_version = (target / ".claude/orchestra-version").read_text(
        encoding="utf-8"
    )
    interrupted = 130
    shim_dir = tmp_path / "shim"
    write_python3_shim(shim_dir, f"exit {interrupted}")

    update_result = run_update(target, template, extra_path=shim_dir)

    assert update_result.returncode == interrupted, update_result.stdout
    assert "stopping the update" in update_result.stderr
    assert "Update Summary" not in update_result.stdout
    assert (target / ".claude/orchestra-version").read_text(
        encoding="utf-8"
    ) == installed_version
    assert find_debris(target) == []


def test_install_stops_when_the_model_step_is_interrupted(tmp_path: Path) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    interrupted = 130
    shim_dir = tmp_path / "shim"
    write_python3_shim(shim_dir, f"exit {interrupted}")

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--yes", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == interrupted, result.stdout
    assert "stopping the installation" in result.stderr
    assert "installation complete" not in result.stdout


def test_update_to_a_template_without_the_manifest_warns_and_succeeds(
    tmp_path: Path,
) -> None:
    """Rolling back to a template ref from before the manifest is a supported
    update, not a failure: there is simply no model step to run."""
    template, target = install_from_template(tmp_path)
    for name in ("models.py", "models.json"):
        (template / ".claude/skills/_shared" / name).unlink()
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "before the manifest"],
        check=True,
    )
    settings_before = (target / SETTINGS_REL).read_bytes()
    config_before = (target / CONFIG_REL).read_bytes()

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert "is not part of this template version" in update_result.stdout
    # Said again in the summary, where it will be read.
    assert "models were not applied" in update_result.stdout.split("Update Summary")[1]
    assert (target / SETTINGS_REL).read_bytes() == settings_before
    assert (target / CONFIG_REL).read_bytes() == config_before


def test_the_gate_accepts_agent_worktrees_and_rejects_other_strays(
    tmp_path: Path,
) -> None:
    """Claude Code checks out worktree-isolated agents under .claude/worktrees/.
    That directory is runtime state: the boundary check must allow it and the
    model-name lint must not read the repository copies inside it. Any other
    unknown entry under .claude/ still fails."""
    template = build_template_repo(tmp_path)
    worktree = template / ".claude/worktrees/agent-example"
    worktree.mkdir(parents=True)
    (worktree / "notes.md").write_text("Use Sonnet 5.5 here.\n", encoding="utf-8")

    with_worktree = run_gate(template)

    assert with_worktree.returncode == 0, with_worktree.stdout + with_worktree.stderr

    (template / ".claude/strays").mkdir()
    (template / ".claude/strays/keep.txt").write_text("x\n", encoding="utf-8")

    with_stray = run_gate(template)

    assert with_stray.returncode == 1, with_stray.stdout
    assert "Unexpected .claude entry: strays" in with_stray.stdout


def test_the_template_gate_rejects_what_only_a_project_may_have(
    tmp_path: Path,
) -> None:
    """In the template nothing is a project choice. A value the manifest does
    not name, or a committed opt-out, is a hand edit that would otherwise ship
    to every new installation with the gate still green. An installed project
    keeps the lenient check: see
    test_update_keeps_custom_model_values_and_reports_them."""
    template = build_template_repo(tmp_path)
    assert not (template / ".claude/orchestra-version").exists()
    baseline = run_gate(template)
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr

    config = template / CONFIG_REL
    pristine = config.read_text(encoding="utf-8")
    config.write_text(
        set_toml_value(pristine, "model", CUSTOM_CODEX_MODEL), encoding="utf-8"
    )
    hand_edited = run_gate(template)
    config.write_text(pristine, encoding="utf-8")

    override = template / MODELS_LOCAL_REL
    override.write_text(
        json.dumps({"schema_version": 1, "manage_native_settings": False}),
        encoding="utf-8",
    )
    opted_out = run_gate(template)
    override.unlink()

    for result in (hand_edited, opted_out):
        assert result.returncode == 1, result.stdout
        assert "FAIL: Model coherence" in result.stdout
        assert "strict: " in result.stdout
    assert CUSTOM_CODEX_MODEL in hand_edited.stdout
    assert run_gate(template).returncode == 0


def test_the_template_gate_rejects_a_model_name_in_prose(tmp_path: Path) -> None:
    """A sentence that names a model is a copy of the manifest nobody updates.
    The gate reports it by file and line, in the template only: once installed,
    a project's README is its own to word."""
    template, target = install_from_template(tmp_path)
    readme = template / "README.md"
    pristine = readme.read_text(encoding="utf-8")
    readme.write_text(pristine + "\nBuilt for Opus 4.6.\n", encoding="utf-8")
    line_number = pristine.count("\n") + 2

    in_template = run_gate(template)
    readme.write_text(pristine, encoding="utf-8")

    assert in_template.returncode == 1, in_template.stdout
    assert "FAIL: Model names only in the manifest" in in_template.stdout
    assert f"lint: README.md:{line_number}: versioned-name" in in_template.stdout
    assert run_gate(template).returncode == 0

    (target / "README.md").write_text("Built for Opus 4.6.\n", encoding="utf-8")
    in_project = run_gate(target)

    assert in_project.returncode == 0, in_project.stdout + in_project.stderr
    assert "Model names only in the manifest" not in in_project.stdout


def test_the_template_is_not_marked_as_an_installed_project() -> None:
    """`.claude/orchestra-version` is how `scripts/check.sh` tells an installed
    project from the template, and so what switches the template-only checks
    off. Committed here, it would turn them off for the template itself."""
    assert not (REPO_ROOT / ".claude/orchestra-version").exists()
    assert not os.path.lexists(REPO_ROOT / ".claude/orchestra-version")


def test_the_gate_fails_a_template_that_carries_the_install_marker(
    tmp_path: Path,
) -> None:
    """The marker must not be able to switch the gate off quietly: with it,
    a model name in the template's own prose would otherwise pass."""
    template = build_template_repo(tmp_path)
    readme = template / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nBuilt for Opus 4.6.\n",
        encoding="utf-8",
    )
    (template / ".claude/orchestra-version").write_text("1.1.0\n", encoding="utf-8")

    result = run_gate(template)

    assert result.returncode == 1, result.stdout
    assert "FAIL: Model names only in the manifest" in result.stdout
    assert ".claude/orchestra-version exists in the template repository" in (
        result.stdout
    )


def test_update_replaces_the_renamed_role_definitions(
    tmp_path: Path, models_module: ModuleType, resolved_models: dict[str, Any]
) -> None:
    """A project installed before the rename holds the two definitions under
    their old names. After an update they must be gone, not left beside the
    new ones: two definitions of one role would split routing between them."""
    template, target = install_from_template(tmp_path)
    renames = models_module.LEGACY_AGENT_NAMES
    agents = target / ".claude/agents"
    for old, new in renames.items():
        (agents / f"{new}.md").rename(agents / f"{old}.md")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert sorted(path.stem for path in agents.glob("*.md")) == sorted(
        resolved_models["claude"]["agents"]
    )
    for old, new in renames.items():
        assert not (agents / f"{old}.md").exists()
        definition = (agents / f"{new}.md").read_text(encoding="utf-8")
        assert f"\nname: {new}\n" in definition
        assigned = resolved_models["claude"]["agents"][new]
        assert f"\nmodel: {assigned}\n" in definition
    assert run_gate(target).returncode == 0
    # The codemod that performed the rename is a template tool, never shipped.
    assert (template / "scripts/rename-roles.py").is_file()
    assert not (target / "scripts/rename-roles.py").exists()


def test_update_refuses_an_override_that_still_uses_an_old_role_name(
    tmp_path: Path, models_module: ModuleType
) -> None:
    """The override names an agent that no longer exists. Ignoring the entry
    would drop the project's model choice without a word, so the update says
    which key to rename, applies nothing, and the project's gate stays red
    until it is fixed."""
    template, target = install_from_template(tmp_path)
    old, new = next(iter(models_module.LEGACY_AGENT_NAMES.items()))
    override_model = "haiku"
    override_path = target / MODELS_LOCAL_REL
    override_path.write_text(
        json.dumps({"schema_version": 1, "claude": {"agents": {old: override_model}}}),
        encoding="utf-8",
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert f"the agent {old} was renamed to {new}" in update_result.stdout
    assert f'rename the key "{old}"' in update_result.stdout
    assert "models.local.json is not in effect" in update_result.stdout
    definition = target / ".claude/agents" / f"{new}.md"
    assert f"\nmodel: {override_model}\n" not in definition.read_text(encoding="utf-8")
    gate = run_gate(target)
    assert gate.returncode == 1
    assert "FAIL: Model coherence" in gate.stdout
    assert f"renamed to {new}" in gate.stdout

    # Renaming the key, as the message says, is the whole fix.
    override_path.write_text(
        json.dumps({"schema_version": 1, "claude": {"agents": {new: override_model}}}),
        encoding="utf-8",
    )
    assert run_update(target, template).returncode == 0
    assert f"\nmodel: {override_model}\n" in definition.read_text(encoding="utf-8")
    assert run_gate(target).returncode == 0


def test_install_ignores_the_model_tools_temporary_files(tmp_path: Path) -> None:
    """A run that is killed outright leaves a temporary copy of a settings
    file behind. It must not be one `git add -A` away from a commit."""
    target = tmp_path / "project"
    init_git_repo(target)

    assert run_install(target).returncode == 0

    for leftover in (
        ".claude/.settings.json.models-tmp.4242",
        ".codex/.config.toml.models-tmp.4242",
        ".claude/agents/.codex-debugger.md.models-tmp.4242",
    ):
        ignored = subprocess.run(
            ["git", "-C", str(target), "check-ignore", "-q", leftover], check=False
        )
        assert ignored.returncode == 0, leftover
    pattern = ".*.models-tmp.*"
    assert (
        pattern in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )


def test_install_ignores_agent_worktrees(tmp_path: Path) -> None:
    """Claude Code checks out worktree-isolated agents under .claude/worktrees/.
    Each is a full copy of the repository, so an installed project must not be
    one `git add -A` away from committing one."""
    target = tmp_path / "project"
    init_git_repo(target)

    assert run_install(target).returncode == 0

    gitignore = (target / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/worktrees/" in gitignore
    ignored = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "check-ignore",
            "-q",
            ".claude/worktrees/agent-example/AGENTS.md",
        ],
        check=False,
    )
    assert ignored.returncode == 0


def test_first_update_by_an_older_updater_is_completed_by_the_second_run(
    tmp_path: Path, resolved_models: dict[str, Any]
) -> None:
    """The rollout case: the run that delivers the manifest is executed by the
    previous `update.sh`, which has no apply phase. It must leave a state the
    project's own gate reports, and a second run must finish the job."""
    template, target = install_from_template(tmp_path)
    updater = target / "scripts/update.sh"
    previous = updater.read_text(encoding="utf-8").replace("    apply_models\n", "", 1)
    assert "    apply_models\n" not in previous
    updater.write_text(previous, encoding="utf-8")
    # An installation from before the manifest has neither the tool nor the
    # data: the first run has to deliver both, or the gate and the Codex
    # wrapper have nothing to read.
    shared = target / ".claude/skills/_shared"
    for name in ("models.py", "models.json"):
        (shared / name).unlink()
    shipped = resolved_models["superseded"]["claude.subagent_default"][0]
    settings = read_settings(target)
    settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = shipped
    (target / SETTINGS_REL).write_text(canonical_json(settings), encoding="utf-8")

    first = run_update(target, template)

    assert first.returncode == 0, first.stderr
    assert "once more" in first.stdout
    assert read_settings(target)["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == shipped
    delivered = subprocess.run(
        ["python3", str(shared / "models.py"), "get", "codex.model"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    assert json.loads(delivered.stdout)["value"] == resolved_models["codex"]["model"]
    check = subprocess.run(
        ["bash", str(target / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 1
    assert "FAIL: Model coherence" in check.stdout
    assert "models.py apply" in check.stdout

    second = run_update(target, template)

    assert second.returncode == 0, second.stderr
    assert (
        read_settings(target)["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
        == resolved_models["claude"]["subagent_default"]
    )
    check = subprocess.run(
        ["bash", str(target / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_install_refuses_parent_symlink_that_escapes_target(tmp_path: Path) -> None:
    target = tmp_path / "project"
    outside = tmp_path / "outside"
    init_git_repo(target)
    outside.mkdir()
    (target / ".claude").symlink_to(outside, target_is_directory=True)

    result = run_install(target)

    assert result.returncode == 2
    assert "symlinked parent" in result.stderr
    assert list(outside.iterdir()) == []


def test_install_refuses_parent_symlink_that_aliases_inside_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    aliased_directory = target / "aliased"
    init_git_repo(target)
    aliased_directory.mkdir()
    (target / ".claude").symlink_to(aliased_directory, target_is_directory=True)

    result = run_install(target)

    assert result.returncode == 2
    assert "symlinked parent" in result.stderr
    assert list(aliased_directory.iterdir()) == []


def test_install_refuses_non_regular_project_file_before_writing(
    tmp_path: Path,
) -> None:
    target = tmp_path / "project"
    init_git_repo(target)
    (target / "CLAUDE.md").mkdir()

    result = run_install(target)

    assert result.returncode == 2
    assert "regular file" in result.stderr
    assert not (target / "AGENTS.md").exists()


def test_update_uses_namespaced_version_file_and_preserves_project_version(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    (target / "VERSION").write_text("9.4.1\n", encoding="utf-8")
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    (template / "VERSION").write_text("0.3.1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(template), "add", "VERSION"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "release 0.3.1"],
        check=True,
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert (target / "VERSION").read_text(encoding="utf-8") == "9.4.1\n"
    assert (target / ".claude/orchestra-version").read_text(
        encoding="utf-8"
    ) == "0.3.1\n"


def test_install_creates_real_runtime_directories(tmp_path: Path) -> None:
    target = tmp_path / "project"
    init_git_repo(target)

    result = run_install(target)

    assert result.returncode == 0, result.stderr
    # The runtime is physically owned by .claude/: nothing here is reached
    # through a link, and nothing is duplicated into .agents/ or .codex/.
    for directory in (".claude", ".agents", ".codex"):
        assert not any(path.is_symlink() for path in (target / directory).iterdir())
    for legacy in ("rules", "skills", "agents", "hooks", "docs"):
        assert not (target / ".agents" / legacy).exists()
    assert (target / ".claude/agents/general-purpose-deep.md").is_file()
    assert (target / ".claude/skills/context-loader/SKILL.md").is_file()
    assert not any(path.is_symlink() for path in (target / ".codex").iterdir())
    assert ".claude/hooks/" in (target / ".claude/settings.json").read_text(
        encoding="utf-8"
    )


def test_update_removes_legacy_agents_runtime_paths(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr
    subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "Test User"], check=True
    )
    subprocess.run(
        ["git", "-C", str(target), "commit", "-qm", "initial install"], check=True
    )

    # Recreate the pre-2.0 shape: the runtime lived under .agents/, and Codex
    # kept a copy of the skills it enabled.
    legacy_runtime = target / ".agents/rules"
    legacy_runtime.mkdir(parents=True)
    (legacy_runtime / "coding-principles.md").write_text("stale\n", encoding="utf-8")
    codex_skills = target / ".codex/skills"
    codex_skills.mkdir()
    (codex_skills / "legacy.md").write_text("legacy\n", encoding="utf-8")
    legacy_project_files = {
        "docs/DESIGN.md": "# Legacy design\n",
        "logs/session.log": "legacy log\n",
        "checkpoints/session.md": "legacy checkpoint\n",
    }
    for relative_path, content in legacy_project_files.items():
        legacy_path = target / ".agents" / relative_path
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(content, encoding="utf-8")
    claude_settings = target / ".claude/settings.json"
    claude_settings.write_text(
        claude_settings.read_text(encoding="utf-8").replace(
            ".claude/hooks/", ".agents/hooks/"
        ),
        encoding="utf-8",
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    # Template content that moved to .claude/ is removed from .agents/, and
    # .codex/skills stays removed (Codex resolves skills through config.toml
    # path= instead).
    assert not legacy_runtime.exists()
    assert not codex_skills.exists()
    assert (target / ".claude/rules").is_dir()
    assert not (target / ".claude/rules").is_symlink()
    assert {path.name for path in (target / ".codex").iterdir()} == {
        "config.toml",
        "AGENTS.md",
    }
    migrated_settings = claude_settings.read_text(encoding="utf-8")
    assert ".claude/hooks/" in migrated_settings
    assert ".agents/hooks/" not in migrated_settings
    # Project-owned data is moved, never deleted.
    for relative_path in ("logs/session.log", "checkpoints/session.md"):
        assert (target / ".claude" / relative_path).read_text(encoding="utf-8") == (
            legacy_project_files[relative_path]
        )
    migration_backups = list(target.glob(".orchestra-backup-native-migration-*"))
    assert len(migration_backups) == 1
    assert (migration_backups[0] / ".agents/docs/DESIGN.md").read_text(
        encoding="utf-8"
    ) == legacy_project_files["docs/DESIGN.md"]


def test_update_migrates_legacy_claude_zones_into_shared_state(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    legacy_state = "## Current Project\n\nKeep this migrated state.\n"
    (target / "CLAUDE.md").write_text(
        "# Old Claude adapter\n\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "# @orchestra:template-boundary\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "## Repository Identity\n\nLegacy project.\n\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "# @orchestra:repo-boundary\n"
        "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + legacy_state,
        encoding="utf-8",
    )
    (target / "AGENTS.md").write_text("# Old CLI bootstrap\n", encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    # The zones are migrated first, then the file that would shadow AGENTS.md is
    # moved aside instead of being deleted or re-installed.
    assert not (target / "CLAUDE.md").exists()
    assert not (target / "CLAUDE.md").is_symlink()
    backup = target / "CLAUDE.md.pre-agents-md.bak"
    assert "Legacy project." in backup.read_text(encoding="utf-8")
    migrated = (target / ".claude/STATE.md").read_text(encoding="utf-8")
    assert "Legacy project." in migrated
    assert legacy_state.strip() in migrated
    assert "Legacy project." not in (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Skill Catalog" in (target / "AGENTS.md").read_text(encoding="utf-8")


def test_update_moves_a_plain_claude_md_aside(tmp_path: Path) -> None:
    """Earlier templates installed a real CLAUDE.md with no boundary markers.
    It has nothing to migrate, but it still shadows AGENTS.md."""
    template = build_template_repo(tmp_path)
    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr
    (target / "CLAUDE.md").write_text("# Old main contract\n", encoding="utf-8")

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert not (target / "CLAUDE.md").exists()
    assert (target / "CLAUDE.md.pre-agents-md.bak").read_text(
        encoding="utf-8"
    ) == "# Old main contract\n"
    assert "instead of AGENTS.md" in update_result.stdout + update_result.stderr


def test_update_leaves_no_stage_and_swap_debris_and_syncs_safe_dirs(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    marker = "# updated marker for debris test\n"
    (template / ".claude/docs/INDEX.md").write_text(
        (template / ".claude/docs/INDEX.md").read_text(encoding="utf-8") + marker,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "bump .agents content"],
        check=True,
    )

    update_result = run_update(target, template)

    assert update_result.returncode == 0, update_result.stderr
    assert find_debris(target) == []
    assert (target / ".claude/docs/INDEX.md").read_text(encoding="utf-8") == (
        template / ".claude/docs/INDEX.md"
    ).read_text(encoding="utf-8")


def test_update_rolls_back_safe_dir_on_mid_swap_failure(tmp_path: Path) -> None:
    template = build_template_repo(tmp_path)

    target = tmp_path / "project"
    init_git_repo(target)
    install_result = run_install(target, script=template / "scripts/install.sh")
    assert install_result.returncode == 0, install_result.stderr

    original_index = (target / ".claude/docs/INDEX.md").read_text(encoding="utf-8")
    original_listing = sorted(
        p.relative_to(target / ".agents") for p in (target / ".agents").rglob("*")
    )

    marker = "# should never reach the target on a rolled-back update\n"
    (template / ".claude/docs/INDEX.md").write_text(
        (template / ".claude/docs/INDEX.md").read_text(encoding="utf-8") + marker,
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(template), "commit", "-qm", "bump .agents content"],
        check=True,
    )

    # Shim `mv` on PATH to fail specifically on the second (staging -> live)
    # rename of the .claude/rules swap, simulating a crash between the two mv
    # calls in sync_safe_dirs(). Every other invocation delegates to the
    # real mv so the rest of the update proceeds normally.
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir()
    mv_shim = shim_dir / "mv"
    mv_shim.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '    if [[ "${arg}" == *".claude/rules.orchestra-staging."* ]]; then\n'
        '        echo "shim: simulated mid-swap mv failure" >&2\n'
        "        exit 1\n"
        "    fi\n"
        "done\n"
        'exec /bin/mv "$@"\n',
        encoding="utf-8",
    )
    mv_shim.chmod(0o755)

    update_result = run_update(target, template, extra_path=shim_dir)

    assert update_result.returncode != 0
    assert find_debris(target) == []
    restored_listing = sorted(
        p.relative_to(target / ".agents") for p in (target / ".agents").rglob("*")
    )
    assert restored_listing == original_listing
    assert (target / ".claude/docs/INDEX.md").read_text(
        encoding="utf-8"
    ) == original_index


def test_a_freshly_installed_project_passes_its_own_consistency_check(
    tmp_path: Path,
) -> None:
    """`scripts/check.sh` ships with the template, so it must pass where it lands.

    It passed in this repository and failed in every downstream project:
    `INDEX.md` names `.claude/docs/plans/`, whose marker file is tracked here
    but was absent from both distribution lists, so check 1 reported
    `Missing: .claude/docs/plans/` on a clean install. A check that only holds
    in the repository that authors it is not a check.
    """
    target = tmp_path / "project"
    init_git_repo(target)

    assert run_install(target).returncode == 0

    result = subprocess.run(
        ["bash", str(target / "scripts/check.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout


# "<destination in a project>": "<pristine seed in the template>".
SEEDED_PROJECT_FILES = {
    ".claude/STATE.md": "scripts/scaffold/STATE.md",
    ".claude/docs/DESIGN.md": "scripts/scaffold/DESIGN.md",
}
LIVE_SENTINEL = b"\n<!-- sentinel: the template repository's own working state -->\n"
SEED_SENTINEL = b"\n<!-- sentinel: the pristine seed -->\n"
PROJECT_SENTINEL = b"\n<!-- sentinel: written by the project itself -->\n"


def append_bytes(path: Path, content: bytes) -> None:
    path.write_bytes(path.read_bytes() + content)


def commit_template(template: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(template), "commit", "-qm", message], check=True)


def diverge_template_state(template: Path) -> dict[str, bytes]:
    """Make the template's own state and design differ from the seeds.

    That is what the template repository looks like once it is developed with
    its own orchestration. Both sides get a different marker, so a test can
    tell which file a project received and not merely that two files differ.
    Returns the seed bytes, keyed by the destination they belong at.
    """
    seeds: dict[str, bytes] = {}
    for destination, seed in SEEDED_PROJECT_FILES.items():
        append_bytes(template / destination, LIVE_SENTINEL)
        append_bytes(template / seed, SEED_SENTINEL)
        seeds[destination] = (template / seed).read_bytes()
    return seeds


def test_install_seeds_a_new_project_from_the_scaffold_not_the_template_s_state(
    tmp_path: Path,
) -> None:
    """The leak this pins: the installer used to copy the template repository's
    own `.claude/STATE.md` and `.claude/docs/DESIGN.md`, so whatever that
    repository recorded about its own development shipped to every project."""
    template = build_template_repo(tmp_path)
    seeds = diverge_template_state(template)
    target = tmp_path / "project"
    init_git_repo(target)

    result = run_install(target, script=template / "scripts/install.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    for destination, seed_bytes in seeds.items():
        installed = (target / destination).read_bytes()
        assert installed == seed_bytes, destination
        assert LIVE_SENTINEL not in installed
        assert f"Installed {destination}" in result.stdout
    # The seeds reach a project only as the two files they seed.
    assert not (target / "scripts/scaffold").exists()
    assert run_gate(target).returncode == 0


def test_install_and_update_never_ship_the_template_s_progress_or_checkpoints(
    tmp_path: Path,
) -> None:
    """The template repository records its own progress in `PROGRESS.md` and
    `.claude/checkpoints/`. Neither is template content: a new project starts
    without them, and an update leaves the project's own untouched."""
    template = build_template_repo(tmp_path)
    (template / "PROGRESS.md").write_bytes(b"# PROGRESS" + LIVE_SENTINEL)
    (template / ".claude/checkpoints/2026-01-01-000000.md").write_bytes(
        b"# Checkpoint" + LIVE_SENTINEL
    )
    commit_template(template, "template progress")
    target = tmp_path / "project"
    init_git_repo(target)

    installed = run_install(target, script=template / "scripts/install.sh")

    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert not (target / "PROGRESS.md").exists()
    assert [p.name for p in (target / ".claude/checkpoints").iterdir()] == []

    own_progress = b"# PROGRESS" + PROJECT_SENTINEL
    (target / "PROGRESS.md").write_bytes(own_progress)
    updated = run_update(target, template)

    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert (target / "PROGRESS.md").read_bytes() == own_progress
    assert [p.name for p in (target / ".claude/checkpoints").iterdir()] == []


def test_install_leaves_a_project_s_own_state_and_design_untouched(
    tmp_path: Path,
) -> None:
    template = build_template_repo(tmp_path)
    diverge_template_state(template)
    target = tmp_path / "project"
    init_git_repo(target)
    own: dict[str, bytes] = {}
    for destination in SEEDED_PROJECT_FILES:
        path = target / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"# Project-owned {destination}\n".encode() + PROJECT_SENTINEL)
        own[destination] = path.read_bytes()

    result = run_install(target, script=template / "scripts/install.sh")

    assert result.returncode == 0, result.stdout + result.stderr
    for destination, content in own.items():
        assert (target / destination).read_bytes() == content, destination
        assert f"Preserved existing {destination}" in result.stdout


@pytest.mark.parametrize("seed", sorted(SEEDED_PROJECT_FILES.values()))
def test_install_refuses_a_template_that_lacks_a_seed(
    tmp_path: Path, seed: str
) -> None:
    """A missing seed is an incomplete template, found before anything is
    written. It is never papered over with the template's own state."""
    template = build_template_repo(tmp_path)
    diverge_template_state(template)
    (template / seed).unlink()
    target = tmp_path / "project"
    init_git_repo(target)

    result = run_install(target, script=template / "scripts/install.sh")

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Template source is incomplete: {seed} is missing." in result.stderr
    assert not (target / ".claude").exists()
    assert not (target / "AGENTS.md").exists()


def test_install_refuses_a_symlinked_parent_of_a_seeded_destination(
    tmp_path: Path,
) -> None:
    """The escape check reads the destination half of each
    "<destination>:<seed>" entry. `.claude/docs/research/` is managed through
    no other list, so only that half can catch a link planted there."""
    target = tmp_path / "project"
    outside = tmp_path / "outside"
    init_git_repo(target)
    outside.mkdir()
    (target / ".claude/docs").mkdir(parents=True)
    (target / ".claude/docs/research").symlink_to(outside, target_is_directory=True)

    result = run_install(target)

    assert result.returncode == 2, result.stdout + result.stderr
    # The whole line: an entry passed through unsplit would be named here as
    # "<destination>:<seed>", which a substring match would still accept.
    assert (
        "[ERROR] Refusing symlinked parent for managed path: "
        ".claude/docs/research/.gitkeep"
    ) in result.stderr.splitlines()
    assert list(outside.iterdir()) == []
    assert not (target / "AGENTS.md").exists()


def test_update_seeds_missing_project_files_from_the_scaffold(tmp_path: Path) -> None:
    template, target = install_from_template(tmp_path)
    seeds = diverge_template_state(template)
    commit_template(template, "the template records its own state")
    for destination in SEEDED_PROJECT_FILES:
        (target / destination).unlink()

    result = run_update(target, template)

    assert result.returncode == 0, result.stdout + result.stderr
    for destination, seed_bytes in seeds.items():
        restored = (target / destination).read_bytes()
        assert restored == seed_bytes, destination
        assert LIVE_SENTINEL not in restored
    assert not (target / "scripts/scaffold").exists()
    assert run_gate(target).returncode == 0


def test_update_never_touches_a_project_s_own_state_and_design(tmp_path: Path) -> None:
    template, target = install_from_template(tmp_path)
    diverge_template_state(template)
    commit_template(template, "the template records its own state")
    own: dict[str, bytes] = {}
    for destination in SEEDED_PROJECT_FILES:
        append_bytes(target / destination, PROJECT_SENTINEL)
        own[destination] = (target / destination).read_bytes()

    # Twice: the first run replaces scripts/update.sh, the second runs it.
    for attempt in ("first", "second"):
        result = run_update(target, template)

        assert result.returncode == 0, attempt + result.stdout + result.stderr
        for destination, content in own.items():
            assert (target / destination).read_bytes() == content, (
                attempt,
                destination,
            )
    assert run_gate(target).returncode == 0


def test_update_from_a_template_without_a_scaffold_uses_the_canonical_paths(
    tmp_path: Path,
) -> None:
    """A template ref from before the seeds moved (an older tag, or another
    repository through ORCHESTRA_TEMPLATE_REPO) keeps the pristine files at the
    destination paths. Updating to it is supported, not an error."""
    template, target = install_from_template(tmp_path)
    shutil.rmtree(template / "scripts/scaffold")
    legacy: dict[str, bytes] = {}
    for destination in SEEDED_PROJECT_FILES:
        append_bytes(template / destination, SEED_SENTINEL)
        legacy[destination] = (template / destination).read_bytes()
    commit_template(template, "a template from before the scaffold")
    for destination in SEEDED_PROJECT_FILES:
        (target / destination).unlink()

    result = run_update(target, template)

    assert result.returncode == 0, result.stdout + result.stderr
    for destination, content in legacy.items():
        assert (target / destination).read_bytes() == content, destination
    assert run_gate(target).returncode == 0


@pytest.mark.parametrize(("destination", "seed"), sorted(SEEDED_PROJECT_FILES.items()))
def test_update_refuses_a_scaffold_that_lacks_a_seed(
    tmp_path: Path, destination: str, seed: str
) -> None:
    """The fallback to the canonical path is for a template with no scaffold at
    all. Where the scaffold exists, that path holds the template repository's
    own state, so a missing seed stops the update instead of shipping it."""
    template, target = install_from_template(tmp_path)
    diverge_template_state(template)
    (template / seed).unlink()
    commit_template(template, "a scaffold with a seed missing")
    (target / destination).unlink()

    result = run_update(target, template)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"Template has no seed for {destination}: {seed} is missing." in (
        result.stderr
    )
    assert not (target / destination).exists()


def test_update_refuses_a_scaffold_path_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    """Only a template with nothing at `scripts/scaffold` predates the seeds.
    A file there is a broken scaffold, not an old template, and must not send
    the update back to the canonical paths."""
    template, target = install_from_template(tmp_path)
    diverge_template_state(template)
    shutil.rmtree(template / "scripts/scaffold")
    (template / "scripts/scaffold").write_text("not a directory\n", encoding="utf-8")
    commit_template(template, "a file where the scaffold belongs")
    for destination in SEEDED_PROJECT_FILES:
        (target / destination).unlink()

    result = run_update(target, template)

    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "Template has no seed for .claude/STATE.md: "
        "scripts/scaffold/STATE.md is missing."
    ) in result.stderr
    for destination in SEEDED_PROJECT_FILES:
        assert not (target / destination).exists()


# --------------------------------------------------------------------------
# The updater never writes through what a project holds
# --------------------------------------------------------------------------

OUTSIDE_TEXT = "the owner's own file, outside the project\n"
LEGACY_ZONES = (
    "# ━━━━━\n# @orchestra:template-boundary\n# ━━━━━\nidentity\n"
    "# ━━━━━\n# @orchestra:repo-boundary\n# ━━━━━\nworking notes\n"
)


def outside_file(tmp_path: Path) -> Path:
    """A file outside the project that no update may change."""
    victim = tmp_path / "outside" / "victim.txt"
    (victim.parent / "a-directory").mkdir(parents=True)
    victim.write_text(OUTSIDE_TEXT, encoding="utf-8")
    return victim


def listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


@pytest.mark.parametrize(
    "shape", ["directory-holding-a-link", "link-to-a-file", "link-to-nowhere"]
)
def test_update_never_writes_a_seed_through_what_is_at_its_path(
    tmp_path: Path, shape: str
) -> None:
    """Anything at a seed's path counts as present. A directory there used to
    be copied into, through a link inside it; a link to nowhere used to be
    written through, creating the file it named."""
    template, target = install_from_template(tmp_path)
    victim = outside_file(tmp_path)
    seed = target / ".claude/STATE.md"
    seed.unlink()
    if shape == "directory-holding-a-link":
        seed.mkdir()
        (seed / "STATE.md").symlink_to(victim)
    elif shape == "link-to-a-file":
        seed.symlink_to(victim)
    else:
        seed.symlink_to(victim.with_name("created-by-the-update.txt"))
    outside = listing(victim.parent)

    result = run_update(target, template)

    assert result.returncode == 0, result.stdout + result.stderr
    assert victim.read_text(encoding="utf-8") == OUTSIDE_TEXT
    assert listing(victim.parent) == outside
    assert seed.is_symlink() or seed.is_dir()


def test_update_never_appends_legacy_state_through_a_link(tmp_path: Path) -> None:
    template, target = install_from_template(tmp_path)
    victim = outside_file(tmp_path)
    state = target / ".claude/STATE.md"
    state.unlink()
    state.symlink_to(victim)
    (target / "CLAUDE.md").write_text(LEGACY_ZONES, encoding="utf-8")

    result = run_update(target, template)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "was NOT migrated into it" in result.stdout
    assert victim.read_text(encoding="utf-8") == OUTSIDE_TEXT


@pytest.mark.parametrize("canonical", ["absent", "present"])
def test_update_refuses_to_migrate_a_legacy_directory_that_holds_links(
    tmp_path: Path, canonical: str
) -> None:
    """The legacy move carried links into `.claude/`, and the steps after it
    wrote through them: a seed created a file outside, and a `.gitkeep`
    landed in a directory outside."""
    template, target = install_from_template(tmp_path)
    victim = outside_file(tmp_path)
    if canonical == "absent":
        shutil.rmtree(target / ".claude/docs")
    legacy = target / ".agents/docs"
    legacy.mkdir(parents=True)
    (legacy / "note.md").write_text("the project's own notes\n", encoding="utf-8")
    (legacy / "DESIGN.md").symlink_to(victim.with_name("created-by-the-update.txt"))
    (legacy / "libraries").symlink_to(victim.parent / "a-directory")
    outside = listing(victim.parent)

    result = run_update(target, template)

    assert result.returncode == 1, result.stdout + result.stderr
    assert ".agents/docs holds a link" in result.stderr
    assert listing(victim.parent) == outside
    assert victim.read_text(encoding="utf-8") == OUTSIDE_TEXT
    # Left where it was, and the rest of the update still happened.
    assert (legacy / "note.md").is_file() and (legacy / "DESIGN.md").is_symlink()
    design = target / ".claude/docs/DESIGN.md"
    keep = target / ".claude/docs/libraries/.gitkeep"
    assert design.is_file() and not design.is_symlink()
    assert keep.is_file() and not keep.parent.is_symlink()


def test_update_never_writes_through_a_link_named_like_its_temporary_file(
    tmp_path: Path,
) -> None:
    """Temporary files used to be named `<destination>.tmp.<process id>`, a
    name anybody can prepare. `exec` keeps the process id, so the links
    planted here carry the very number the updater then runs as."""
    template, target = install_from_template(tmp_path)
    victim = outside_file(tmp_path)
    plant_then_update = (
        "for rel in AGENTS.md .claude/docs/INDEX.md .claude/orchestra-version "
        '.claude/settings.json; do ln -s "$1" "$rel.tmp.$$"; done; '
        "exec bash scripts/update.sh --yes"
    )

    result = subprocess.run(
        ["bash", "-c", plant_then_update, "plant", str(victim)],
        check=False,
        capture_output=True,
        text=True,
        cwd=target,
        env={**os.environ, "ORCHESTRA_TEMPLATE_REPO": str(template)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert victim.read_text(encoding="utf-8") == OUTSIDE_TEXT
    assert not list(target.rglob(".orchestra-tmp.*"))


def test_update_stops_at_a_directory_where_a_template_file_belongs(
    tmp_path: Path,
) -> None:
    template, target = install_from_template(tmp_path)
    contract = target / "AGENTS.md"
    contract.unlink()
    contract.mkdir()
    (contract / "mine.md").write_text("the owner's\n", encoding="utf-8")

    result = run_update(target, template)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Cannot write AGENTS.md: a directory is in the way." in result.stderr
    assert listing(contract) == ["mine.md"]
    assert not list(target.rglob(".orchestra-tmp.*"))


def test_update_keeps_the_mode_of_the_files_it_replaces_with_the_template_s(
    tmp_path: Path,
) -> None:
    """A file put in place by temporary file and rename takes the template
    file's mode, not the private mode a temporary file is born with."""
    template, target = install_from_template(tmp_path)

    result = run_update(target, template)

    assert result.returncode == 0, result.stdout + result.stderr
    for rel in ("AGENTS.md", ".claude/orchestra-version", "scripts/check.sh"):
        wanted = (template / ("VERSION" if "version" in rel else rel)).stat().st_mode
        assert (target / rel).stat().st_mode & 0o777 == wanted & 0o777, rel
    assert os.access(target / "scripts/update.sh", os.X_OK)

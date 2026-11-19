"""Fixtures shared by the tests that compare a location to the model manifest.

No test hard-codes a model name it expects the template to use: a model bump
edits ``models.json`` and nothing else, so every such assertion reads the
resolved manifest instead.

Also here, because it must hold for every test file: git isolation. Many tests
build scratch repositories and run git in them. Each test runs with the
developer's git configuration and git environment taken away
(``isolated_git``), so a setting such as ``merge.renames=false`` cannot fail a
test, and an exported ``GIT_DIR`` cannot make one write into another
repository.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / ".claude" / "skills" / "_shared"
MODELS_SCRIPT = SHARED_DIR / "models.py"
MODELS_LINT_SCRIPT = SHARED_DIR / "models_lint.py"

# Variables that point git at another repository, index or work tree, or that
# inject configuration. With any of them inherited, a test's `git commit` in a
# scratch directory can land somewhere else entirely.
GIT_REDIRECTS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
)


@pytest.fixture(scope="session")
def empty_git_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty file to stand in for the developer's global git configuration."""
    path = tmp_path_factory.mktemp("git-isolation") / "gitconfig"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, empty_git_config: Path) -> None:
    """Run every test's git with no global or system configuration and no
    inherited repository, index or work tree. Child processes inherit it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in GIT_REDIRECTS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def models_module() -> ModuleType:
    """The bundled ``models.py``, imported from its real location."""
    spec = importlib.util.spec_from_file_location("orchestra_models", MODELS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolve their own module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def resolved_models(models_module: ModuleType) -> dict[str, Any]:
    """The template's manifest as every consumer resolves it."""
    return models_module.resolve(REPO_ROOT)


@pytest.fixture(scope="session")
def models_lint_module() -> ModuleType:
    """The bundled ``models_lint.py``, imported from its real location.

    It imports its sibling ``models`` the way it does when run as a script,
    so its own directory is on the path while it loads.
    """
    spec = importlib.util.spec_from_file_location(
        "orchestra_models_lint", MODELS_LINT_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(SHARED_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SHARED_DIR))
    return module

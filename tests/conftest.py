"""Fixtures shared by the tests that compare a location to the model manifest.

No test hard-codes a model name it expects the template to use: a model bump
edits ``models.json`` and nothing else, so every such assertion reads the
resolved manifest instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_SCRIPT = REPO_ROOT / ".claude" / "skills" / "_shared" / "models.py"


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

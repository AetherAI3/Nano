"""Shared pytest wiring for the q-search tests.

Two optional environment variables point at sibling checkouts that are not
installed packages: ``NANO_QSEARCH_ATLAS_PATH`` (the ``aether-atlas`` directory
that contains the ``aether_atlas`` package) and ``NANO_QSEARCH_PREDATOR_PATH``
(a Predator-M checkout, used only by the integration test). Both are added to
``sys.path`` when set (appended, so Nano wins name collisions); a value that is
not a directory raises at collection. Nothing is imported here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "qsearch_fixtures"

for _var in ("NANO_QSEARCH_ATLAS_PATH", "NANO_QSEARCH_PREDATOR_PATH"):
    _path = os.environ.get(_var)
    if not _path:
        continue
    if not Path(_path).is_dir():
        raise RuntimeError(f"{_var} does not point at a directory: {_path}")
    if _path not in sys.path:
        sys.path.append(_path)      # appended, so Nano's own packages win any name collision


def pytest_configure(config):
    config.addinivalue_line("markers", "engine: needs qiskit-aer and aether_atlas (system Python, PowerShell)")
    config.addinivalue_line("markers", "integration: needs NANO_QSEARCH_PREDATOR_PATH")


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES

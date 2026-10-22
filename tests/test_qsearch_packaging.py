import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QUANTUM_MODULES = ("qiskit", "qiskit_aer", "numpy", "aether_atlas")


def test_quantum_extra_and_script_declared():
    tomllib = pytest.importorskip("tomllib")
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []
    extra = data["project"]["optional-dependencies"]["quantum"]
    assert any(d.startswith("qiskit==2.3.1") for d in extra) and any(d.startswith("qiskit-aer==0.17.2") for d in extra)
    assert data["project"]["scripts"]["nano-qsearch-serve"] == "nano_qsearch.service:main"
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    assert any(fnmatch("nano_qsearch", pattern) for pattern in include)     # the service package ships in the wheel
    markers = data["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("engine:") for m in markers) and any(m.startswith("integration:") for m in markers)


def test_base_modules_import_without_quantum_packages():
    # A fresh interpreter, so modules other tests already imported cannot hide a top-level quantum import.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import nano_qsearch.service, nano_qsearch.runtime_mps, nano_qsearch.runtime_ibm;"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {QUANTUM_MODULES!r});"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_docs_mention_every_env_var():
    text = (ROOT / "docs" / "qsearch-service.md").read_text(encoding="utf-8")
    for var in ("NANO_QSEARCH_BIND", "NANO_QSEARCH_BACKEND", "NANO_QSEARCH_DATA_DIR", "NANO_QSEARCH_KEY_PATH",
                "NANO_QSEARCH_REQUEST_KEY_PATH", "NANO_QSEARCH_ATLAS_PATH", "NANO_QSEARCH_PYTHON",
                "NANO_QSEARCH_MAX_VARIABLES", "NANO_QSEARCH_MAX_BODY_BYTES", "NANO_QSEARCH_MAX_SHOTS",
                "NANO_QSEARCH_MAX_CHI", "NANO_QSEARCH_MAX_CONCURRENT", "NANO_QSEARCH_REQUEST_TIMEOUT_S",
                "X-Aether-Qsearch-Auth", "AETHER_AQRC_HW_GO", "AETHER_AQRC_AUTHORITY_PATH"):
        assert var in text, var

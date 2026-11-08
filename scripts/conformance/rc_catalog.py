"""Declared suites and capabilities of the release-conformance harness.

The catalog is static: which suites exist, which mode they belong to, whether
they are required, which test files and fixtures they consume and which test
IDs must pass. ``resolve_suites`` pins it against one exported source tree
(file lists and SHA-256 values), producing the manifest's suite entries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from rc_common import sha256_file

PYTEST_KINDS = ("pytest",)


@dataclass(frozen=True)
class SuiteSpec:
    id: str
    mode: str
    kind: str
    required: bool
    capability: Optional[str] = None
    files: Tuple[str, ...] = ()
    partition: bool = False
    required_tests: Tuple[str, ...] = ()
    fixtures: Tuple[str, ...] = ()
    needs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    issue: str
    required: bool
    summary: str


@dataclass(frozen=True)
class Catalog:
    suites: Tuple[SuiteSpec, ...]
    capabilities: Tuple[CapabilitySpec, ...] = field(default=())
    origin_modules: Tuple[str, ...] = ("nano", "nano_qsearch")

    def suite(self, suite_id: str) -> SuiteSpec:
        for spec in self.suites:
            if spec.id == suite_id:
                return spec
        raise KeyError(suite_id)


ISSUES = "https://github.com/AetherAI3/Nano/issues/"

NATIVE_REQUIRED_TESTS = (
    "tests/test_quantum_native_roundtrip.py::test_native_source_to_verified_records_through_artifact_and_local_service",
    "tests/test_quantum_native_roundtrip.py::test_400_recipients_1200_pubs_survive_artifact_load_and_http_local_service",
    "tests/test_quantum_native_roundtrip.py::test_caller_retained_identity_rejects_substitution_even_when_service_records_agree",
    "tests/test_quantum_native_roundtrip.py::test_hardware_seal_or_accounting_claims_and_partial_records_are_rejected",
    "tests/test_quantum_native_roundtrip.py::test_reordered_pubs_are_rejected_by_the_caller",
    "tests/test_quantum_native_roundtrip.py::test_core_checking_and_caller_verification_work_without_optional_dependencies",
    "tests/test_quantum_native_artifact.py::test_pinned_build_is_deterministic_and_matches_frozen_goldens",
    "tests/test_quantum_native_artifact.py::test_missing_duplicate_and_reordered_pubs_fail",
    "tests/test_quantum_native_artifact.py::test_substituted_or_tampered_artifact_is_refused_by_retained_digest_and_contents",
    "tests/test_quantum_native_artifact.py::test_corrupted_qpy_metadata_isa_and_drift_fail_before_execution",
    "tests/test_quantum_native_contract.py::test_large_pub_map_binds_every_recipient_chunk_to_its_pub",
    "tests/test_quantum_native_contract.py::test_native_capability_cannot_name_hardware",
    "tests/test_qsearch_native_service.py::test_native_routes_are_not_enabled_by_default_before_auth",
    "tests/test_qsearch_native_service.py::test_fake_runtime_round_trip_status_and_replay_never_run_twice",
    "tests/test_qsearch_native_service.py::test_stale_evidence_bank_is_refused_by_the_host_registry",
    "tests/test_qsearch_native_service.py::test_lost_acknowledgement_stays_held_and_restart_reattaches_without_rerun",
    "tests/test_qsearch_native_service.py::test_http_routes_with_the_actual_local_runtime",
)

QUBO_REQUIRED_TESTS = (
    "tests/test_quantum_contract.py",
    "tests/test_quantum_adapter.py",
)

# Modules that skip at collection unless a caller-supplied ATLAS directory or
# Predator checkout is importable (see tests/conftest.py).
LIFECYCLE = "protocol/qsearch-lifecycle/v1/"
LIFECYCLE_FIXTURES = (LIFECYCLE + "*.json", LIFECYCLE + "CONTRACT.md", LIFECYCLE + "SHA256SUMS",
                      LIFECYCLE + "fixtures/*.json")
LIFECYCLE_REQUIRED_TESTS = (
    "tests/test_qsearch_lifecycle_wire_contract.py::test_regen_check_reproduces_committed_fixtures_byte_for_byte",
    "tests/test_qsearch_lifecycle_wire_contract.py::test_sha256sums_cover_every_v1_file_and_verify",
    "tests/test_qsearch_lifecycle_wire_contract.py::test_signing_vector_reproduces_python_client_bytes_and_verifies",
    "tests/test_qsearch_lifecycle_wire_contract.py::test_history_validates_and_python_client_parses_it_field_by_field",
    "tests/test_qsearch_lifecycle_wire_contract.py::test_error_body_maps_to_expected_metadata",
    "tests/test_qsearch_lifecycle_wire_contract.py::test_negative_expected_error_is_what_the_contract_mandates",
)
TS = "sdk/typescript/qsearch-lifecycle/"

CROSS_REPO_FILES = (
    "tests/test_qsearch_compile.py",
    "tests/test_qsearch_compile_device.py",
    "tests/test_qsearch_compile_options.py",
    "tests/test_qsearch_confirmed_zero_atlas.py",
    "tests/test_qsearch_engine.py",
    "tests/test_qsearch_hw0_atlas.py",
    "tests/test_qsearch_ibm_fakefez.py",
    "tests/test_qsearch_lifecycle_atlas.py",
    "tests/test_qsearch_predator_integration.py",
    "tests/test_qsearch_v2_predator.py",
)


def _core_suites() -> List[SuiteSpec]:
    return [
        SuiteSpec("core.contribution", "core", "contribution", True,
                  files=("scripts/check_contribution.py",)),
        SuiteSpec("core.cli_help", "core", "cli_help", True),
        SuiteSpec("core.import_without_optional_sdks", "core", "import_blocked", True),
        SuiteSpec("core.pytest", "core", "pytest", True, partition=True),
        SuiteSpec("wheel.build", "core", "wheel_build", True, needs=("wheel_builder",)),
        SuiteSpec("wheel.contents", "core", "wheel_contents", True, needs=("wheel",)),
        SuiteSpec("wheel.import_without_optional_sdks", "core", "wheel_import", True, needs=("wheel",)),
        SuiteSpec("wheel.cli_help", "core", "wheel_cli_help", True, needs=("wheel",)),
        SuiteSpec("wheel.native_fixture", "core", "wheel_native_fixture", True, "native_bridge",
                  needs=("wheel",)),
        SuiteSpec("wheel.package_cli", "core", "wheel_package_cli", True, "package_cli",
                  fixtures=("examples/agent-package/*",), needs=("wheel",)),
    ]


def _capability_suites() -> List[SuiteSpec]:
    return [
        SuiteSpec("cap.package_cli.pytest", "core", "pytest", True, "package_cli",
                  files=("tests/test_package_cli.py", "tests/test_package_cases.py",
                         "tests/test_package_test_cli.py"),
                  fixtures=("examples/agent-package/*",)),
        SuiteSpec("cap.records.pytest", "core", "pytest", True, "records",
                  files=("tests/test_records.py", "tests/test_records_compat.py",
                         "tests/test_records_runtime.py"),
                  fixtures=("tests/fixtures/records/*",)),
        SuiteSpec("cap.lifecycle_wire.sha256sums", "core", "sha256sums", True, "lifecycle_wire",
                  files=(LIFECYCLE + "SHA256SUMS",)),
        SuiteSpec("cap.lifecycle_wire.regen_check", "core", "regen_check", True, "lifecycle_wire",
                  files=(LIFECYCLE + "fixtures/regen.py",), fixtures=LIFECYCLE_FIXTURES),
        SuiteSpec("cap.lifecycle_wire.pytest", "core", "pytest", True, "lifecycle_wire",
                  files=("tests/test_qsearch_lifecycle_wire_contract.py", "tests/test_qsearch_lifecycle_client.py"),
                  required_tests=LIFECYCLE_REQUIRED_TESTS, fixtures=LIFECYCLE_FIXTURES),
        SuiteSpec("cap.ts_client.build", "core", "ts_build", False, "ts_client",
                  files=(TS + "package.json", TS + "package-lock.json", TS + "tsconfig.json"),
                  fixtures=(TS + "src/*.ts",), needs=("node:0", "npm_cache")),
        SuiteSpec("cap.ts_client.node.primary", "core", "node", False, "ts_client",
                  files=(TS + "package.json",), fixtures=(TS + "test/*.test.mjs",),
                  needs=("node:0", "ts_build")),
        SuiteSpec("cap.ts_client.node.secondary", "core", "node", False, "ts_client",
                  files=(TS + "package.json",), fixtures=(TS + "test/*.test.mjs",),
                  needs=("node:1", "ts_build")),
        SuiteSpec("cap.proposal_benchmark.pytest", "core", "pytest", True, "proposal_benchmark",
                  files=("tests/test_proposal_benchmark_contract.py", "tests/test_proposal_benchmark.py",
                         "tests/test_proposal_benchmark_repair.py"),
                  fixtures=("benchmarks/proposal/manifest.json", "benchmarks/proposal/policy.json",
                            "benchmarks/proposal/data/*.json", "benchmarks/proposal/reports/*")),
    ]


def _qiskit_suites() -> List[SuiteSpec]:
    return [
        SuiteSpec("qiskit.local_compile", "qiskit", "pytest", True, "qubo_artifact",
                  files=("tests/test_quantum_contract.py", "tests/test_quantum_adapter.py",
                         "tests/test_quantum_prepare.py", "tests/test_quantum_prepared_runtime.py",
                         "tests/test_qsearch_provenance.py"),
                  required_tests=QUBO_REQUIRED_TESTS, needs=("qiskit_pin",)),
        SuiteSpec("qiskit.native_bridge", "qiskit", "pytest", True, "native_bridge",
                  files=("tests/test_quantum_native_contract.py", "tests/test_quantum_native_artifact.py",
                         "tests/test_quantum_native_roundtrip.py", "tests/test_qsearch_native_service.py"),
                  required_tests=NATIVE_REQUIRED_TESTS,
                  fixtures=("nano/quantum/fixtures/native-*.json", "nano/quantum/schemas/native-*.json"),
                  needs=("qiskit_pin",)),
    ]


def _cross_repo_suites() -> List[SuiteSpec]:
    return [
        SuiteSpec("cross_repo.fake_provider", "cross_repo", "pytest", False, "cross_repo_lifecycle",
                  files=CROSS_REPO_FILES, needs=("checkouts",)),
    ]


CAPABILITIES = (
    CapabilitySpec("package_cli", ISSUES + "84", True,
                   "nano package check/build/test from source and from the wheel"),
    CapabilitySpec("records", ISSUES + "85", True, "bounded immutable records"),
    CapabilitySpec("lifecycle_wire", ISSUES + "86", True,
                   "q-search lifecycle wire v1: SHA256SUMS, regen --check, Python contract and client tests"),
    CapabilitySpec("ts_client", ISSUES + "86", False, "read-only TypeScript status client (offline lockfile install, tsc, node --test)"),
    CapabilitySpec("proposal_benchmark", ISSUES + "87", True, "frozen proposal benchmark reports replay"),
    CapabilitySpec("qubo_artifact", ISSUES + "88", True,
                   "QUBO prepared artifact load/profile/target/tamper/default-off gates (pinned Qiskit)"),
    CapabilitySpec("native_bridge", ISSUES + "61", True,
                   "native prepare/sample plan -> artifact -> local service -> verified result"),
    CapabilitySpec("cross_repo_lifecycle", ISSUES + "88", False,
                   "fake-provider lifecycle/accounting/provenance against caller-supplied checkouts"),
)


def default_catalog() -> Catalog:
    suites = _core_suites() + _capability_suites() + _qiskit_suites() + _cross_repo_suites()
    return Catalog(tuple(suites), CAPABILITIES)


def _glob(src: Path, patterns: Iterable[str]) -> List[str]:
    found = set()
    for pattern in patterns:
        found.update(p.relative_to(src).as_posix() for p in src.glob(pattern) if p.is_file())
    return sorted(found)


def _pinned(src: Path, paths: Sequence[str]) -> list:
    rows = []
    for rel in paths:
        path = src / rel
        rows.append({"path": rel, "sha256": sha256_file(path) if path.is_file() else None})
    return rows


def _partition(src: Path, catalog: Catalog) -> List[str]:
    claimed = {f for spec in catalog.suites if spec.kind == "pytest" for f in spec.files}
    every = _glob(src, ("tests/test_*.py",))
    return [path for path in every if path not in claimed]


def resolve_suites(catalog: Catalog, src: Path) -> list:
    """Pin every declared suite against the exported source tree ``src``."""
    entries = []
    for spec in catalog.suites:
        files = _partition(src, catalog) if spec.partition else list(spec.files)
        entries.append({
            "id": spec.id,
            "mode": spec.mode,
            "kind": spec.kind,
            "required": spec.required,
            "capability": spec.capability,
            "files": _pinned(src, files),
            "fixture_patterns": list(spec.fixtures),
            "fixtures": _pinned(src, _glob(src, spec.fixtures)),
            "required_tests": list(spec.required_tests),
            "needs": list(spec.needs),
        })
    return entries

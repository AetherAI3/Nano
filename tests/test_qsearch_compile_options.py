import json
from dataclasses import replace

import pytest

pytestmark = pytest.mark.engine
pytest.importorskip("qiskit")
pytest.importorskip("aether_atlas")

from nano_qsearch.compile import (BASIS_PREP_ENV, LAYOUT_BACKEND_ENV, LAYOUT_ENV, CompileOptions,  # noqa: E402
                                  compile_program, diagonal_ground, rank_qubits)
from nano_qsearch.job import parse_job                      # noqa: E402

ENV_NAMES = (BASIS_PREP_ENV, LAYOUT_ENV, LAYOUT_BACKEND_ENV)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def four(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_4var.json").read_text(encoding="utf-8")))


@pytest.fixture
def pair02(fixtures_dir):
    return parse_job(json.loads((fixtures_dir / "job_pair02.json").read_text(encoding="utf-8")))


def _diagonal(job, diag):
    n = len(diag)
    q = tuple(tuple(float(diag[i]) if i == j else 0.0 for j in range(n)) for i in range(n))
    return replace(job, q=q, variables=tuple(f"x{i}" for i in range(n)))


def _ideal_bits(plan) -> set[str]:
    from qiskit.quantum_info import Statevector
    probs = Statevector.from_instruction(plan.logical.remove_final_measurements(inplace=False)).probabilities_dict()
    return {k for k, p in probs.items() if p > 1e-9}


def _snapshot(good: list[int], n: int = 156) -> dict:
    """A synthetic 156-qubit snapshot entry: ``good`` qubits read at ~0.1 % (in that order), the rest at 5 %;
    qubit 0 has no T1."""
    readout = {str(q): 0.05 for q in range(n)}
    for rank, q in enumerate(good):
        readout[str(q)] = 0.001 + 1e-5 * rank
    return {"num_qubits": n, "last_update": "2026-01-01 00:00:00+00:00", "readout_error": readout,
            "one_qubit_error": {"x": {str(q): 3e-4 for q in range(n)}, "sx": {str(q): 3e-4 for q in range(n)}},
            "t1_s": {str(q): (None if q == 0 else 1.5e-4) for q in range(n)}}


# -- pure helpers ---------------------------------------------------------------------------------------------------
def test_diagonal_ground_is_the_sign_vector_and_refuses_couplings_or_zero_fields():
    assert diagonal_ground([[-1.0, 0.0], [0.0, 2.0]]) == (1, 0)
    assert diagonal_ground([[-1.0, 0.5], [0.5, 2.0]]) is None
    assert diagonal_ground([[-1.0, 0.0], [0.0, 0.0]]) is None
    with pytest.raises(ValueError):
        diagonal_ground([[float("nan"), 0.0], [0.0, 1.0]])     # never silently read as a 0 bit


def test_rank_qubits_orders_by_cost_and_skips_uncalibrated_qubits():
    errors = {0: (0.001, 1e-4, None), 1: (0.010, 1e-4, 1e-4), 2: (0.002, 1e-4, 1e-4), 3: (0.002, 1e-4, 1e-4),
              4: (float("nan"), 1e-4, 1e-4), 5: (1.0, 1e-4, 1e-4), 6: (0.002, 5e-3, 1e-4)}
    assert rank_qubits(errors, 3) == [2, 3, 6]          # 0: no T1, 4: nan, 5: error 1.0; 6 costs more than 2, 3
    with pytest.raises(ValueError):
        rank_qubits(errors, 5)


def test_from_env_parses_and_fails_closed(tmp_path):
    assert CompileOptions.from_env({}) == CompileOptions() and not CompileOptions().enabled
    assert CompileOptions.from_env({BASIS_PREP_ENV: "1"}).basis_prep
    assert CompileOptions.from_env({LAYOUT_ENV: "target"}).layout == "target"
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"backends": {"ibm_fez": _snapshot(list(range(10, 26)))}}), encoding="utf-8")
    opts = CompileOptions.from_env({LAYOUT_ENV: f"calibration:{path}", LAYOUT_BACKEND_ENV: "ibm_fez"})
    assert opts.layout == "calibration" and opts.layout_backend == "ibm_fez" and len(opts.calibration_sha256) == 64
    for bad in ({BASIS_PREP_ENV: "yes"}, {LAYOUT_ENV: "best"}, {LAYOUT_ENV: f"calibration:{tmp_path / 'none.json'}"},
                {LAYOUT_ENV: "target", LAYOUT_BACKEND_ENV: "ibm_kingston"}, {LAYOUT_BACKEND_ENV: "ibm_fez"}):
        with pytest.raises(ValueError):
            CompileOptions.from_env(bad)
    with pytest.raises(ValueError):
        CompileOptions(layout="calibration")             # a snapshot is required
    with pytest.raises(ValueError):
        CompileOptions(layout="target", layout_backend="ibm_fez")   # would be ignored, so it is refused


# -- default stays the default --------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["job_4var.json", "job_pair02.json"])
def test_options_off_keep_the_compile_and_digest(fixtures_dir, name):
    job = parse_job(json.loads((fixtures_dir / name).read_text(encoding="utf-8")))
    a, b = compile_program(job), compile_program(job, options=CompileOptions())
    assert a.compiled_plan_digest == b.compiled_plan_digest and "rewrites" not in a.compile_record


# -- basis_prep -----------------------------------------------------------------------------------------------------
def test_basis_prep_prepares_the_unique_minimiser_whatever_the_angles(four):
    job = _diagonal(four, [-1.0, 1.0, -0.5, 2.0])            # minimiser x = (1, 0, 1, 0)
    plan = compile_program(job, options=CompileOptions(basis_prep=True))
    assert plan.compile_record["rewrites"] == {"basis_prep": "applied", "layout": {"mode": "off"}}
    assert _ideal_bits(plan) == {"0101"}                    # qiskit order: x3 x2 x1 x0
    assert set(plan.isa.count_ops()) <= {"x", "measure"} and plan.metrics["native_2q"] == 0
    other = compile_program(replace(job, gammas=(1.234,), betas=(-0.7,)), options=CompileOptions(basis_prep=True))
    assert other.compiled_plan_digest == plan.compiled_plan_digest
    default = compile_program(job)                          # QAOA at (0.4, 0.4) does not output the minimiser
    assert _ideal_bits(default) != {"0101"}


def test_basis_prep_leaves_coupled_qubos_and_explicit_joint5_alone(pair02):
    plain = compile_program(pair02)
    plan = compile_program(pair02, options=CompileOptions(basis_prep=True))
    assert plan.compile_record["rewrites"]["basis_prep"] == "not_applicable"
    assert plan.inspection == plain.inspection and plan.variant == plain.variant
    assert plan.compiled_plan_digest != plain.compiled_plan_digest     # the record says an option was on
    joint = compile_program(replace(pair02, variant="joint5"), options=CompileOptions(basis_prep=True))
    assert joint.variant == "joint5"


def test_compile_program_reads_the_environment_when_no_options_are_passed(four, monkeypatch):
    job = _diagonal(four, [-1.0, 1.0, -0.5, 2.0])
    monkeypatch.setenv(BASIS_PREP_ENV, "1")
    assert compile_program(job).compile_record["rewrites"]["basis_prep"] == "applied"


# -- layout (Fez targets) -------------------------------------------------------------------------------------------
def test_calibration_layout_puts_bit_i_on_the_ith_best_qubit(four):
    pytest.importorskip("qiskit_ibm_runtime")
    good = [100 + 2 * i for i in range(4)]
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target="fake_fez")
    opts = CompileOptions(basis_prep=True, layout="calibration", calibration={"ibm_fez": _snapshot(good)},
                          calibration_sha256="ab" * 32, layout_backend="ibm_fez")
    plan = compile_program(job, options=opts)
    layout = plan.compile_record["rewrites"]["layout"]
    assert layout["applied"] and layout["physical"] == good and layout["calibration_sha256"] == "ab" * 32
    assert plan.inspection["initial_layout"] == good
    assert sorted(plan.inspection["measurement_map"], key=lambda r: r[1]) == [[q, i] for i, q in enumerate(good)]


def test_calibration_layout_refuses_a_snapshot_without_the_backend(four):
    pytest.importorskip("qiskit_ibm_runtime")
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target="fake_fez")
    opts = CompileOptions(layout="calibration", calibration={"ibm_kingston": _snapshot([1, 2, 3, 4])})
    with pytest.raises(ValueError):
        compile_program(job, options=opts)


@pytest.mark.parametrize("target", ["fake_fez", "ibm_fez"])
def test_calibration_layout_refuses_another_devices_entry(four, target):
    pytest.importorskip("qiskit_ibm_runtime")
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target=target)
    opts = CompileOptions(layout="calibration", calibration={"ibm_kingston": _snapshot([1, 2, 3, 4])},
                          layout_backend="ibm_kingston")              # same width, another device's errors
    with pytest.raises(ValueError, match="may not rank"):
        compile_program(job, options=opts)


def test_calibration_layout_refuses_a_malformed_entry(four):
    pytest.importorskip("qiskit_ibm_runtime")
    entry = {k: v for k, v in _snapshot([1, 2, 3, 4]).items() if k != "t1_s"}
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target="fake_fez")
    opts = CompileOptions(layout="calibration", calibration={"fake_fez": entry})
    with pytest.raises(ValueError, match="mappings"):
        compile_program(job, options=opts)


def test_target_layout_ranks_by_the_targets_own_errors_and_skips_entangled_circuits(four, pair02):
    pytest.importorskip("qiskit_ibm_runtime")
    plan = compile_program(replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target="fake_fez"),
                           options=CompileOptions(basis_prep=True, layout="target"))
    layout = plan.compile_record["rewrites"]["layout"]
    assert layout["applied"] and plan.inspection["initial_layout"] == layout["physical"]
    assert layout["errors_digest"].startswith("sha256:")               # names the calibration the target carried
    coupled = compile_program(replace(pair02, target="fake_fez"), options=CompileOptions(layout="target"))
    assert coupled.compile_record["rewrites"]["layout"] == {"mode": "target", "applied": False,
                                                            "reason": "circuit has two-qubit gates"}


def test_layout_is_not_applied_off_the_fez_register(four):
    plan = compile_program(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), options=CompileOptions(layout="target"))
    assert plan.compile_record["rewrites"]["layout"]["applied"] is False      # heron_heavy_hex has no calibration


# -- with the HW-0 device compile (live backend) --------------------------------------------------------------------
def test_live_backend_compile_composes_with_basis_prep_and_layout(four):
    pytest.importorskip("qiskit_ibm_runtime")
    from types import SimpleNamespace

    from qiskit_ibm_runtime.fake_provider import FakeFez

    from nano_qsearch.compile import TARGET_METADATA_KEY, check_isa
    live = SimpleNamespace(name="ibm_fez", target=FakeFez().target)        # stands in for the resolved backend
    job = replace(_diagonal(four, [-1.0, 1.0, -0.5, 2.0]), target="ibm_fez")
    plan = compile_program(job, backend=live, options=CompileOptions(basis_prep=True, layout="target"))
    rewrites = plan.compile_record["rewrites"]
    assert rewrites["basis_prep"] == "applied" and rewrites["layout"]["applied"]
    assert plan.inspection["initial_layout"] == rewrites["layout"]["physical"]
    assert plan.compile_record["backend_name"] == "ibm_fez" and TARGET_METADATA_KEY in plan.isa.metadata
    check_isa(plan.isa, live.target)
    good = [100 + 2 * i for i in range(4)]
    snap = compile_program(job, backend=live, options=CompileOptions(
        basis_prep=True, layout="calibration", calibration={"ibm_fez": _snapshot(good)}, calibration_sha256="cd" * 32))
    assert snap.inspection["initial_layout"] == good and snap.compile_record["rewrites"]["layout"]["backend"] == "ibm_fez"
    off = compile_program(job, backend=live)
    assert "rewrites" not in off.compile_record and off.compile_record["backend_name"] == "ibm_fez"

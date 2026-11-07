"""Offline proposal-and-repair benchmark for agent-facing Nano (issue #87).

The benchmark scores recorded candidate proposals against hand-written task
expectations. It performs no model call, network access or provider work.
"""

DATASET_SCHEMA = "nano.benchmark.proposal.dataset.v1"
CASE_SCHEMA = "nano.benchmark.proposal.case.v1"
MANIFEST_SCHEMA = "nano.benchmark.proposal.manifest.v1"
REPORT_SCHEMA = "nano.benchmark.proposal.report.v1"
TELEMETRY_SCHEMA = "nano.benchmark.proposal.telemetry.v1"
POLICY_SCHEMA = "nano.benchmark.proposal.repair-policy.v1"
RUNNER_VERSION = "1.0.0"

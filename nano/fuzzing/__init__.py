"""Permanent deterministic adversarial harness for Nano source and IR."""

from .campaign import (
    CampaignResult,
    Defect,
    canonical_json,
    execution_summary,
    load_document,
    run_campaign,
)
from .generators import (
    EquivalentPrograms,
    InvalidProgram,
    RuntimeComparison,
    ValidProgram,
    generate_equivalent_programs,
    generate_invalid_programs,
    generate_valid_programs,
    render_strategy,
)
from .probes import (
    CatalogAudit,
    ProbeFailure,
    ReceiptAudit,
    RiskAudit,
    audit_catalog_corpus,
    audit_receipt_boundaries,
    audit_risk_boundaries,
)

__all__ = [
    "CampaignResult",
    "CatalogAudit",
    "Defect",
    "EquivalentPrograms",
    "InvalidProgram",
    "ProbeFailure",
    "ReceiptAudit",
    "RuntimeComparison",
    "RiskAudit",
    "ValidProgram",
    "audit_catalog_corpus",
    "audit_receipt_boundaries",
    "audit_risk_boundaries",
    "canonical_json",
    "execution_summary",
    "generate_equivalent_programs",
    "generate_invalid_programs",
    "generate_valid_programs",
    "load_document",
    "render_strategy",
    "run_campaign",
]

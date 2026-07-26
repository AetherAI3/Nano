"""Experimental Nano++ loop primitives.

This package exposes loop-document validation, mutation-admission helpers, and
a deterministic simulator protocol. It does not execute an autonomous loop.
"""

from .graph import KNOWN_LOOP_EFFECTS, LoopGraph
from .mutation import AdmissionDecision, Candidate, admit_mutation
from .nodes import LOOP_STAGES, LoopNode, LoopValidationError
from .quantum import QuantumJob, QuantumResult, QuantumRuntime, SimulatorRuntime

__all__ = [
    "AdmissionDecision",
    "Candidate",
    "KNOWN_LOOP_EFFECTS",
    "LOOP_STAGES",
    "LoopGraph",
    "LoopNode",
    "LoopValidationError",
    "QuantumJob",
    "QuantumResult",
    "QuantumRuntime",
    "SimulatorRuntime",
    "admit_mutation",
]

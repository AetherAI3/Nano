"""Deterministic project discovery: tags, search plans, groups, and capsules.

Nano derives annotations over host-supplied records. Hosts own identity,
authorization, freshness, indexing persistence, GitHub, and memory-graph writes.
"""
from .catalog import DEFAULT_VOCABULARY, VERSION, ProjectError, Vocabulary, canonical_json
from .index import ProjectIndex
from .query import Diagnostic, Predicate, ProjectQuery, compile_query
from .records import ProjectRecord, classify_record

__all__ = ["DEFAULT_VOCABULARY", "VERSION", "Diagnostic", "Predicate", "ProjectError",
           "ProjectIndex", "ProjectQuery", "ProjectRecord", "Vocabulary",
           "canonical_json", "classify_record", "compile_query"]

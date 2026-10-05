"""
V2 data models for the MySQL DBA Validator.

This module provides structured dataclasses used by the upcoming V2
analysis pipeline. It is intentionally additive and does not change
existing runtime behavior. The models are lightweight containers for
analysis results, findings, evidence gaps and recommendations.

Phase 1 (foundation): add models here and keep V1.1 behavior intact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Finding:
    """An actionable finding discovered during analysis.

    severity: e.g. "LOW", "MEDIUM", "HIGH", "CRITICAL"
    title: short summary
    description: longer explanation and suggested verification steps
    """

    severity: str
    title: str
    description: str


@dataclass
class EvidenceGap:
    """Represents evidence that is missing to raise analysis confidence.

    name: machine-friendly key (e.g. "affected_row_count")
    reason: why the evidence is missing (e.g. "no database connection")
    suggested_check: optional human-safe SQL or action the DBA can run
    """

    name: str
    reason: str
    suggested_check: Optional[str] = None


@dataclass
class Recommendation:
    """A recommended DBA action or verification step.

    text: human-readable instruction
    rationale: optional short justification
    """

    text: str
    rationale: Optional[str] = None


@dataclass
class RiskDimension:
    """One dimension of risk (data, execution, scope, schema, privilege).

    name: e.g. "Data Risk"
    level: e.g. "LOW", "MEDIUM", "HIGH", "UNKNOWN"
    reason: optional explanation
    """

    name: str
    level: str
    reason: Optional[str] = None


@dataclass
class RiskAssessment:
    """Structured risk assessment result for a statement.

    overall_score: int
    risk_level: str
    confidence: str

    dimensions: List[RiskDimension] = field(default_factory=list)

    findings: List[Finding] = field(default_factory=list)

    evidence_available: List[str] = field(default_factory=list)
    evidence_missing: List[EvidenceGap] = field(default_factory=list)

    recommendations: List[Recommendation] = field(default_factory=list)
    checklist: List[str] = field(default_factory=list)
"""

    overall_score: int
    risk_level: str
    confidence: str

    dimensions: List[RiskDimension] = field(default_factory=list)

    findings: List[Finding] = field(default_factory=list)

    evidence_available: List[str] = field(default_factory=list)
    evidence_missing: List[EvidenceGap] = field(default_factory=list)

    recommendations: List[Recommendation] = field(default_factory=list)
    checklist: List[str] = field(default_factory=list)


@dataclass
class StatementAnalysis:
    """High-level container tying parsed facts to a risk assessment.

    statement_type and tables are duplicated for convenient access
    without importing the parser types in all modules.
    """

    raw_sql: str
    index: int
    statement_type: str
    tables: List[str]

    assessment: Optional[RiskAssessment] = None


# Backwards-compatible helper: basic factory from minimal inputs.
def minimal_assessment(score: int, confidence: str = "LIMITED") -> RiskAssessment:
    """Create a minimal RiskAssessment with sensible defaults."""

    level = "LOW"

    if score >= 80:
        level = "CRITICAL"
    elif score >= 50:
        level = "HIGH"
    elif score >= 20:
        level = "MEDIUM"

    return RiskAssessment(
        overall_score=score,
        risk_level=level,
        confidence=confidence,
    )

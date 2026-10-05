"""
Recommendation helpers

Centralize generation of Recommendation objects from EvidenceGap inputs so
analyzer.py can remain focused on analysis logic.
"""
from __future__ import annotations

from typing import List

from . import models


def recommendations_from_gaps(gaps: List[models.EvidenceGap]) -> List[models.Recommendation]:
    """Return Recommendation objects derived from evidence gaps.

    For now this mirrors the existing analyzer behavior: whenever an
    EvidenceGap suggests a concrete check (suggested_check), produce a
    Recommendation with that SQL and the gap.reason as the rationale.
    """

    recs: List[models.Recommendation] = []

    for gap in gaps:
        if gap.suggested_check:
            recs.append(models.Recommendation(text=gap.suggested_check, rationale=gap.reason))

    return recs

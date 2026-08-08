"""Choosing an applier for a job."""

from __future__ import annotations

from ..db import ApplyRoute, Job
from .ats.greenhouse import GreenhouseApplier
from .ats.lever import LeverApplier
from .base import FormApplier
from .board import CutshortApplier, InstahyreApplier, LinkedInEasyApplyApplier

_BY_ROUTE: dict[ApplyRoute, type[FormApplier]] = {
    ApplyRoute.ATS_GREENHOUSE: GreenhouseApplier,
    ApplyRoute.ATS_LEVER: LeverApplier,
}

_BY_SOURCE: dict[str, type[FormApplier]] = {
    "linkedin": LinkedInEasyApplyApplier,
    "instahyre": InstahyreApplier,
    "cutshort": CutshortApplier,
}


def applier_for(job: Job) -> FormApplier | None:
    """The applier that can handle this job, or None if it must go manual."""
    if cls := _BY_ROUTE.get(job.apply_route):
        return cls()
    if job.apply_route is ApplyRoute.BOARD_NATIVE:
        if cls := _BY_SOURCE.get(job.source):
            return cls()
    return None


def applier_for_route(route: ApplyRoute) -> FormApplier | None:
    """Applier for a route discovered mid-flight (after following a link)."""
    cls = _BY_ROUTE.get(route)
    return cls() if cls else None

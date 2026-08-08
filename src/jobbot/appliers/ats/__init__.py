"""Applicant tracking system detection and per-ATS appliers."""

from .detect import ATS_LABELS, detect_ats, detect_from_dom, detect_from_url

__all__ = ["ATS_LABELS", "detect_ats", "detect_from_dom", "detect_from_url"]

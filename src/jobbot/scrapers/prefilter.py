"""Rule-based filtering, applied before any LLM call.

This is the cheapest filter in the pipeline and typically removes 40-60% of
raw results. Two places it runs:

- **stub level** (title/company/location) during discovery, so we never spend
  a rate-limited detail-page view on a job we'd throw away.
- **description level** after hydration, for rules that need the full JD.
"""

from __future__ import annotations

import re

from ..config import PrefilterConfig
from .base import JobStub

# "8+ years", "minimum 5 years", "5-7 years of experience"
_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)\b[^.]{0,40}?"
    r"(?:experience|exp\b)",
    re.IGNORECASE,
)


def stub_rejection(stub: JobStub, cfg: PrefilterConfig) -> str | None:
    """Reason to drop this stub, or None to keep it."""
    title = stub.title.lower()
    for keyword in cfg.exclude_title_keywords:
        if keyword.lower() in title:
            return f"title contains {keyword!r}"

    company = stub.company.lower().strip()
    for excluded in cfg.exclude_companies:
        if excluded.lower().strip() == company:
            return f"company {stub.company!r} is excluded"

    if cfg.allow_locations:
        # A remote job passes regardless of the stated location.
        if not stub.remote:
            location = stub.location.lower()
            if not any(allowed.lower() in location for allowed in cfg.allow_locations):
                return f"location {stub.location!r} not in allow list"

    return None


def min_years_required(description: str) -> int | None:
    """Lowest experience requirement stated in the JD, best effort.

    Takes the *minimum* across all matches: a JD saying "3+ years backend,
    8+ years preferred" has a real bar of 3, and rejecting it on the 8 would
    be wrong.
    """
    matches = [int(m.group(1)) for m in _YEARS.finditer(description)]
    plausible = [y for y in matches if 0 < y <= 40]
    return min(plausible) if plausible else None


def description_rejection(description: str, cfg: PrefilterConfig) -> str | None:
    """Reason to drop based on the full JD, or None to keep."""
    if cfg.max_years_required is not None:
        years = min_years_required(description)
        if years is not None and years > cfg.max_years_required:
            return f"requires {years}+ years (cap is {cfg.max_years_required})"
    return None

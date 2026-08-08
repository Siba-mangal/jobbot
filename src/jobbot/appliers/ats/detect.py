"""Which applicant tracking system is this?

Detection runs in two passes. The URL is checked first because it's free and
usually decisive. When it isn't — companies routinely serve Greenhouse from
`careers.acme.com` behind a proxy, or embed it in an iframe — the rendered
DOM carries unmistakable fingerprints.

Getting this right matters in one direction more than the other: mistaking
Workday for Greenhouse produces a half-filled multi-page wizard, which is far
worse than correctly filing it under manual. So every rule here demands a
positive signal, and anything unrecognized falls through to manual.
"""

from __future__ import annotations

import re

from ...db import ApplyRoute

ATS_LABELS = {
    ApplyRoute.ATS_GREENHOUSE: "Greenhouse",
    ApplyRoute.ATS_LEVER: "Lever",
    ApplyRoute.ATS_OTHER: "Manual portal",
    ApplyRoute.BOARD_NATIVE: "Board apply",
    ApplyRoute.UNKNOWN: "Unknown",
}

# Host substrings → route. Checked in order; first match wins.
_HOST_RULES: tuple[tuple[str, ApplyRoute, str], ...] = (
    ("boards.greenhouse.io", ApplyRoute.ATS_GREENHOUSE, "greenhouse"),
    ("job-boards.greenhouse.io", ApplyRoute.ATS_GREENHOUSE, "greenhouse"),
    ("greenhouse.io", ApplyRoute.ATS_GREENHOUSE, "greenhouse"),
    ("jobs.lever.co", ApplyRoute.ATS_LEVER, "lever"),
    ("lever.co", ApplyRoute.ATS_LEVER, "lever"),
    # Recognized, deliberately not automated — multi-page wizards and heavy
    # anti-automation. These go to the manual queue with a direct link.
    ("myworkdayjobs.com", ApplyRoute.ATS_OTHER, "workday"),
    ("workday.com", ApplyRoute.ATS_OTHER, "workday"),
    ("taleo.net", ApplyRoute.ATS_OTHER, "taleo"),
    ("icims.com", ApplyRoute.ATS_OTHER, "icims"),
    ("successfactors.com", ApplyRoute.ATS_OTHER, "successfactors"),
    ("smartrecruiters.com", ApplyRoute.ATS_OTHER, "smartrecruiters"),
    ("workable.com", ApplyRoute.ATS_OTHER, "workable"),
    ("ashbyhq.com", ApplyRoute.ATS_OTHER, "ashby"),
    ("bamboohr.com", ApplyRoute.ATS_OTHER, "bamboohr"),
    ("jobvite.com", ApplyRoute.ATS_OTHER, "jobvite"),
    ("recruitee.com", ApplyRoute.ATS_OTHER, "recruitee"),
    ("teamtailor.com", ApplyRoute.ATS_OTHER, "teamtailor"),
    ("breezy.hr", ApplyRoute.ATS_OTHER, "breezy"),
    ("zohorecruit.com", ApplyRoute.ATS_OTHER, "zoho"),
    ("darwinbox.com", ApplyRoute.ATS_OTHER, "darwinbox"),
    ("keka.com", ApplyRoute.ATS_OTHER, "keka"),
)

# DOM fingerprints, for proxied or embedded boards the URL doesn't reveal.
_DOM_RULES: tuple[tuple[re.Pattern, ApplyRoute, str], ...] = (
    (re.compile(r"grnhse_app|greenhouse\.io/embed|id=[\"']grnhse", re.I),
     ApplyRoute.ATS_GREENHOUSE, "greenhouse"),
    (re.compile(r"boards\.greenhouse\.io|greenhouse-job-board", re.I),
     ApplyRoute.ATS_GREENHOUSE, "greenhouse"),
    (re.compile(r"lever\.co/|data-qa=[\"']application-form|postings-btn", re.I),
     ApplyRoute.ATS_LEVER, "lever"),
    (re.compile(r"myworkdayjobs|workday-", re.I), ApplyRoute.ATS_OTHER, "workday"),
    (re.compile(r"icims", re.I), ApplyRoute.ATS_OTHER, "icims"),
    (re.compile(r"smartrecruiters", re.I), ApplyRoute.ATS_OTHER, "smartrecruiters"),
    (re.compile(r"ashbyhq", re.I), ApplyRoute.ATS_OTHER, "ashby"),
    (re.compile(r"workable", re.I), ApplyRoute.ATS_OTHER, "workable"),
)


def detect_from_url(url: str) -> tuple[ApplyRoute, str]:
    """(route, ats name) from the URL alone."""
    lowered = (url or "").lower()
    if not lowered:
        return ApplyRoute.UNKNOWN, ""
    for needle, route, name in _HOST_RULES:
        if needle in lowered:
            return route, name
    return ApplyRoute.UNKNOWN, ""


def detect_from_dom(html: str) -> tuple[ApplyRoute, str]:
    """(route, ats name) from page markup.

    Only the head of the document is inspected — a careers page that merely
    *links* to a Greenhouse board somewhere in its footer shouldn't be
    mistaken for one.
    """
    head = (html or "")[:60_000]
    if not head:
        return ApplyRoute.UNKNOWN, ""
    for pattern, route, name in _DOM_RULES:
        if pattern.search(head):
            return route, name
    return ApplyRoute.UNKNOWN, ""


def detect_ats(url: str, html: str = "") -> tuple[ApplyRoute, str]:
    """Best guess at the ATS behind a page. URL first, then DOM."""
    route, name = detect_from_url(url)
    if route is not ApplyRoute.UNKNOWN:
        return route, name
    return detect_from_dom(html)

"""XHR JSON capture.

Every one of these boards is a single-page app that renders from an internal
JSON API. Reading that JSON is dramatically more stable than scraping the
rendered DOM — markup and CSS classes churn constantly, API response shapes
rarely do — so scrapers try JSON first and fall back to the DOM.

`JsonSniffer` also backs `jobbot inspect`, which dumps everything a page
fetched so selectors can be calibrated against the live site quickly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Page, Response


@dataclass
class Captured:
    url: str
    status: int
    payload: Any

    def summary(self) -> str:
        shape = _shape(self.payload)
        return f"[{self.status}] {self.url}\n    {shape}"


def _shape(value: Any, depth: int = 0) -> str:
    """One-line description of a JSON payload's structure."""
    if depth > 2:
        return "..."
    if isinstance(value, dict):
        keys = list(value)[:12]
        inner = ", ".join(keys)
        more = f", +{len(value) - len(keys)} more" if len(value) > len(keys) else ""
        return f"dict({inner}{more})"
    if isinstance(value, list):
        if not value:
            return "list(empty)"
        return f"list[{len(value)}] of {_shape(value[0], depth + 1)}"
    return type(value).__name__


class JsonSniffer:
    """Records JSON responses whose URL contains any of `patterns`."""

    def __init__(self, page: Page, patterns: tuple[str, ...] = ("/api/",)):
        self.page = page
        self.patterns = patterns
        self.captured: list[Captured] = []
        self._handler = self._on_response
        page.on("response", self._handler)

    def _on_response(self, response: Response) -> None:
        url = response.url
        if not any(p in url for p in self.patterns):
            return
        content_type = (response.headers or {}).get("content-type", "")
        if "json" not in content_type.lower():
            return
        try:
            payload = response.json()
        except Exception:
            return
        self.captured.append(Captured(url=url, status=response.status, payload=payload))

    def detach(self) -> None:
        try:
            self.page.remove_listener("response", self._handler)
        except Exception:
            pass

    def clear(self) -> None:
        self.captured.clear()

    # ----------------------------------------------------------------
    # Extraction
    # ----------------------------------------------------------------

    def find_records(self, *, required_keys: tuple[str, ...], min_count: int = 1) -> list[dict]:
        """Pull the first list-of-dicts that looks like job records.

        Rather than hardcoding an endpoint path (which changes), this searches
        every captured payload for a list whose members carry the keys a job
        record would have. That survives the API being renamed or moved.
        """
        best: list[dict] = []
        for cap in self.captured:
            for candidate in _iter_record_lists(cap.payload):
                if len(candidate) < min_count:
                    continue
                sample = candidate[0]
                if not any(_has_key(sample, key) for key in required_keys):
                    continue
                if len(candidate) > len(best):
                    best = candidate
        return best

    def dump(self) -> str:
        if not self.captured:
            return "(no JSON responses captured)"
        return "\n".join(c.summary() for c in self.captured)

    def save(self, path) -> None:
        payload = [{"url": c.url, "status": c.status, "payload": c.payload} for c in self.captured]
        path.write_text(json.dumps(payload, indent=2, default=str))


def _iter_record_lists(value: Any, depth: int = 0):
    """Yield every list-of-dicts found anywhere in a JSON payload."""
    if depth > 4:
        return
    if isinstance(value, list):
        if value and all(isinstance(item, dict) for item in value):
            yield value
        for item in value[:5]:
            yield from _iter_record_lists(item, depth + 1)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_record_lists(item, depth + 1)


def _has_key(record: dict, key: str) -> bool:
    """Case-insensitive key presence, one level deep."""
    lowered = {k.lower() for k in record}
    return key.lower() in lowered


def get_path(record: dict, *candidates: str, default: Any = "") -> Any:
    """First non-empty value among candidate keys.

    Supports dotted paths ("company.name") and is case-insensitive at each
    level, so a scraper survives `companyName` becoming `company_name`.
    """
    for candidate in candidates:
        value: Any = record
        for part in candidate.split("."):
            if not isinstance(value, dict):
                value = None
                break
            match = next((k for k in value if k.lower() == part.lower()), None)
            value = value.get(match) if match is not None else None
            if value is None:
                break
        if value not in (None, "", [], {}):
            return value
    return default

"""Setup state for the control panel.

Everything the pipeline needs before it can run, and whether it's there yet:
a logged-in session per site, a filled-in profile, a resume, an API key.

Note what is deliberately absent: any handling of job-portal passwords. Sites
are connected by opening a real browser and letting you log in yourself —
credentials never reach this application, and there is nothing here that could
store them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select

from ..browser.session import has_profile
from ..config import (
    CONFIG_DIR,
    DATA_DIR,
    Profile,
    anthropic_api_key,
    load_profile,
    load_search_config,
)
from ..db import Job, Run, SiteState, utcnow
from ..scrapers.registry import SCRAPERS

PROFILE_PATH = CONFIG_DIR / "profile.yaml"
RESUME_DIR = DATA_DIR
ALLOWED_RESUME_SUFFIXES = {".pdf", ".docx", ".doc", ".txt", ".md"}
MAX_RESUME_BYTES = 10 * 1024 * 1024


@dataclass
class SiteStatus:
    site: str
    enabled: bool
    connected: bool
    jobs_found: int
    paused_until: datetime | None
    pause_reason: str
    last_run: datetime | None
    last_error: str

    @property
    def state(self) -> str:
        if self.paused_until:
            return "paused"
        if not self.connected:
            return "disconnected"
        if not self.enabled:
            return "off"
        return "ready"

    @property
    def blurb(self) -> str:
        # Branches, not a dict — a dict literal would format `paused_until`
        # even when it's None.
        state = self.state
        if state == "paused":
            return f"Paused until {self.paused_until:%b %d, %H:%M UTC} — {self.pause_reason}"
        if state == "disconnected":
            return "Not connected. Click Connect and log in in the browser window."
        if state == "off":
            return "Connected, but disabled in config/search.yaml."
        return "Connected and enabled."


def site_statuses() -> list[SiteStatus]:
    from ..db import session_scope

    cfg = load_search_config()
    out: list[SiteStatus] = []

    with session_scope() as session:
        for site in sorted(SCRAPERS):
            site_cfg = cfg.sites.get(site)
            state = session.get(SiteState, site)

            paused_until = None
            if state and state.paused_until:
                paused = state.paused_until
                if paused.tzinfo is None:
                    paused = paused.replace(tzinfo=UTC)
                if paused > utcnow():
                    paused_until = paused

            jobs = session.execute(
                select(Job).where(Job.source == site).limit(10_000)
            ).scalars()
            count = sum(1 for _ in jobs)

            last = session.execute(
                select(Run)
                .where(Run.kind == "discover", Run.site == site)
                .order_by(Run.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            out.append(
                SiteStatus(
                    site=site,
                    enabled=bool(site_cfg and site_cfg.enabled),
                    connected=has_profile(site),
                    jobs_found=count,
                    paused_until=paused_until,
                    pause_reason=(state.pause_reason if state else "") or "",
                    last_run=last.started_at if last else None,
                    last_error=(last.error if last else "") or "",
                )
            )
    return out


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


@dataclass
class Readiness:
    profile_ok: bool
    profile_missing: list[str]
    resume_ok: bool
    resume_name: str
    api_key_ok: bool
    any_site_connected: bool

    @property
    def can_discover(self) -> bool:
        return self.any_site_connected

    @property
    def can_score(self) -> bool:
        return self.resume_ok and self.api_key_ok

    @property
    def can_apply(self) -> bool:
        return self.profile_ok and self.resume_ok


def readiness() -> Readiness:
    try:
        profile = load_profile()
        missing = profile.missing_required_fields()
        resume = profile.resume_file()
    except FileNotFoundError:
        profile, missing, resume = None, ["config/profile.yaml does not exist yet"], None
    except Exception as exc:
        profile, missing, resume = None, [f"profile.yaml is invalid: {exc}"], None

    resume_ok = bool(resume and resume.exists())
    return Readiness(
        profile_ok=profile is not None and not missing,
        profile_missing=[m for m in missing if "resume" not in m.lower()],
        resume_ok=resume_ok,
        resume_name=resume.name if resume_ok else "",
        api_key_ok=bool(anthropic_api_key()),
        any_site_connected=any(has_profile(site) for site in SCRAPERS),
    )


# --------------------------------------------------------------------------
# Profile read / write
# --------------------------------------------------------------------------


def current_profile() -> Profile:
    """The saved profile, or an empty one to start from."""
    try:
        return load_profile()
    except Exception:
        return Profile()


def save_profile(form: dict[str, str]) -> Profile:
    """Validate a submitted form and write config/profile.yaml.

    Only the fields the form owns are written; the resume path and any
    standard answers already on disk are preserved.
    """
    existing = current_profile()

    def val(key: str, default: str = "") -> str:
        return (form.get(key) or default).strip()

    def flag(key: str) -> bool:
        return form.get(key) in ("on", "true", "True", "1", "yes")

    payload = {
        "identity": {
            "first_name": val("first_name"),
            "last_name": val("last_name"),
            "email": val("email"),
            "phone": val("phone"),
            "city": val("city"),
            "country": val("country"),
        },
        "links": {
            "linkedin": val("linkedin"),
            "github": val("github"),
            "portfolio": val("portfolio"),
        },
        "employment": {
            "current_company": val("current_company"),
            "current_title": val("current_title"),
            "total_years_experience": _as_float(val("total_years_experience")),
            "notice_period_days": _as_int(val("notice_period_days")),
            "current_ctc": val("current_ctc"),
            "expected_ctc": val("expected_ctc"),
        },
        "eligibility": {
            "authorized_to_work_in": [
                c.strip() for c in val("authorized_to_work_in").split(",") if c.strip()
            ],
            "requires_visa_sponsorship": flag("requires_visa_sponsorship"),
            "willing_to_relocate": flag("willing_to_relocate"),
            "preferred_work_mode": val("preferred_work_mode"),
        },
        "documents": existing.documents.model_dump(),
        "standard_answers": existing.standard_answers,
    }

    profile = Profile.model_validate(payload)  # raises on bad input
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True)
    )
    load_profile.cache_clear()
    return profile


def _as_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------


class ResumeError(ValueError):
    pass


def save_resume(filename: str, data: bytes) -> Path:
    """Store an uploaded resume and point the profile at it."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_RESUME_SUFFIXES:
        raise ResumeError(
            f"{suffix or 'that file type'} isn't supported. "
            f"Use one of: {', '.join(sorted(ALLOWED_RESUME_SUFFIXES))}"
        )
    if not data:
        raise ResumeError("The uploaded file was empty.")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeError(f"That file is larger than {MAX_RESUME_BYTES // 1024 // 1024} MB.")

    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    # Always a fixed name, never the uploaded one — a filename from a browser
    # upload has no business becoming a path. Keeping one canonical resume
    # also avoids ambiguity about which file is live.
    target = RESUME_DIR / f"resume{suffix}"
    for other_suffix in ALLOWED_RESUME_SUFFIXES - {suffix}:
        stale = RESUME_DIR / f"resume{other_suffix}"
        if stale.exists():
            stale.unlink(missing_ok=True)
    target.write_bytes(data)

    profile = current_profile()
    profile.documents.resume_path = str(target.relative_to(CONFIG_DIR.parent))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(
        yaml.safe_dump(profile.model_dump(), sort_keys=False, allow_unicode=True)
    )
    load_profile.cache_clear()

    # Re-extract now so a bad PDF surfaces here rather than mid-scoring.
    from ..resume import get_resume_text

    get_resume_text(target, refresh=True)
    return target

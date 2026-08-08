"""Review dashboard.

The one place a human is required. Nothing is ever submitted without passing
through the approve gate here.

Deliberately server-rendered with plain forms and no CDN dependency — this is
a local tool that should work on a plane, and a build step for a job queue
would be silly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..appliers.runner import applications_today
from ..config import SearchQuery, SiteConfig, load_search_config, save_search_config
from ..db import (
    Application,
    ApplyRoute,
    AppStatus,
    Job,
    Score,
    init_db,
    session_scope,
    utcnow,
)
from ..scrapers.registry import SCRAPERS
from . import setup as setup_mod
from .tasks import TASKS, TaskBusy, stream_lines

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


def shell_context(_: Request) -> dict:
    """Values the page chrome needs on *every* render.

    A context processor rather than a line in each handler: the header shows
    these on all pages, and threading them through ten routes is exactly how
    one ends up missing and rendering a blank bar. It also supplies `counts`,
    which three handlers were not passing.

    Note context processors run last and override handler context, so this
    must not invent a value a handler is entitled to disagree with — `counts`
    is the same `_counts()` the handlers compute, not a second opinion.
    """
    task = TASKS.current  # a property, and already None unless still running
    running = task.label if task else ""
    cfg = load_search_config()
    with session_scope() as session:
        counts = _counts(session)
        # The same helper the applier uses to enforce the cap, so the header
        # can never advertise a different limit from the one in force.
        used = applications_today(session)
    return {
        "counts": counts,
        "run_label": f"running {running}" if running else "idle",
        "cap_line": (
            f"{counts.get('approved', 0)} queued · "
            f"{used} of {cfg.apply.max_per_day} daily applications used"
        ),
    }


app = FastAPI(title="jobbot", docs_url=None, redoc_url=None, lifespan=lifespan)
# Vendored webfont, served locally so the UI has no third-party dependency.
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES), context_processors=[shell_context])


ROUTE_LABELS = {
    ApplyRoute.BOARD_NATIVE: ("Board apply", "ok"),
    ApplyRoute.ATS_GREENHOUSE: ("Greenhouse", "ok"),
    ApplyRoute.ATS_LEVER: ("Lever", "ok"),
    ApplyRoute.ATS_OTHER: ("Manual portal", "warn"),
    ApplyRoute.UNKNOWN: ("Unknown", "muted"),
}


def _counts(session) -> dict[str, int]:
    rows = session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    counts = {status.value: count for status, count in rows}
    counts["total"] = sum(counts.values())
    # The nav badge for Review. A discovered-but-unscored job is still a job
    # awaiting review — counting only SCORED here put a "Review 0" badge above
    # a page listing dozens of them, which reads as "nothing to do". This is
    # the same bucket the home donut labels "To review".
    counts["to_review"] = counts.get("new", 0) + counts.get("scored", 0)
    return counts


# ==========================================================================
# Home — the landing dashboard
# ==========================================================================

# Pipeline buckets shown in the donut, in reading order. Colours are the
# validated set from the dataviz palette: three categorical hues that clear
# CVD and normal-vision separation in BOTH light and dark under all-pairs
# (a donut wraps, so first and last touch). "To review" takes the neutral
# de-emphasis grey rather than a fourth hue — at four hues no ordering
# passes both modes, and it is context rather than a call to action.
PIPELINE_BUCKETS = (
    ("to_review", "To review", (AppStatus.NEW, AppStatus.SCORED), "neutral"),
    ("needs_you", "Needs you", (AppStatus.NEEDS_INPUT, AppStatus.MANUAL), "warn"),
    ("queued", "Queued to apply", (AppStatus.APPROVED, AppStatus.APPLYING), "queued"),
    ("submitted", "Submitted", (AppStatus.SUBMITTED,), "done"),
)


def _pipeline(session) -> dict:
    """Counts per bucket, plus the geometry each donut segment needs."""
    rows = session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    by_status = {status: count for status, count in rows}

    segments = []
    for key, label, statuses, tone in PIPELINE_BUCKETS:
        value = sum(by_status.get(s, 0) for s in statuses)
        segments.append({"key": key, "label": label, "value": value, "tone": tone})

    active = sum(s["value"] for s in segments)

    # Arc geometry. r and stroke are fixed in the template; a 2px surface gap
    # separates touching segments, per the mark spec.
    radius, gap = 70.0, 2.0
    circumference = 2 * 3.141592653589793 * radius
    offset = 0.0
    for segment in segments:
        fraction = (segment["value"] / active) if active else 0.0
        length = max(fraction * circumference - gap, 0.0)
        segment["dash"] = round(length, 2)
        segment["gap_rest"] = round(circumference - length, 2)
        segment["offset"] = round(-offset, 2)
        segment["pct"] = round(fraction * 100)
        offset += fraction * circumference

    return {
        "segments": segments,
        "active": active,
        "circumference": round(circumference, 2),
        "closed": by_status.get(AppStatus.SKIPPED, 0) + by_status.get(AppStatus.FAILED, 0),
        "submitted": by_status.get(AppStatus.SUBMITTED, 0),
        "to_review": next(s["value"] for s in segments if s["key"] == "to_review"),
        "queued": next(s["value"] for s in segments if s["key"] == "queued"),
        "needs_you": next(s["value"] for s in segments if s["key"] == "needs_you"),
    }


@app.get("/")
def home(request: Request):
    with session_scope() as session:
        counts = _counts(session)
        pipeline = _pipeline(session)
        top = list(
            session.execute(
                select(Job)
                .options(joinedload(Job.score))
                .join(Score, Score.job_id == Job.id)
                .where(Job.status == AppStatus.SCORED)
                .order_by(Score.fit_score.desc())
                .limit(3)
            )
            .unique()
            .scalars()
        )

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "counts": counts,
            "pipeline": pipeline,
            "top": top,
            "ready": setup_mod.readiness(),
        },
    )


#: Windows offered by the freshness chips, in hours.
FRESH_WINDOWS = {"1h": 1, "24h": 24, "7d": 168}

#: Pseudo-status meaning "still worth looking at" — everything except the two
#: terminal states. The freshness views default to it.
PENDING = "pending"


def _still_open(stmt):
    return stmt.where(Job.status.not_in([AppStatus.SKIPPED, AppStatus.SUBMITTED]))


def _within(stmt, fresh: str):
    hours = FRESH_WINDOWS[fresh]
    return stmt.where(Job.posted_at.is_not(None)).where(
        Job.posted_at >= utcnow() - timedelta(hours=hours)
    )


def _fresh_counts(session) -> dict[str, int]:
    """How many jobs were posted within each window, for the filter chips.

    This must stay in lockstep with what clicking the chip actually shows.
    They diverged once — the chips counted every fresh job while the table
    still applied the scored-only default and the score threshold, so a chip
    reading "last hour 29" opened an empty page.
    """
    return {
        key: session.execute(
            _within(_still_open(select(func.count(Job.id))), key)
        ).scalar()
        or 0
        for key in FRESH_WINDOWS
    }


def humanize_age(when) -> str:
    """"12m ago" / "3h ago" / "2d ago". Empty when the source gave no date."""
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = (utcnow() - when).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 604800:
        return f"{int(seconds // 86400)}d ago"
    return when.strftime("%b %d")


templates.env.filters["age"] = humanize_age


@app.get("/review")
def index(
    request: Request,
    status: str | None = None,
    source: str = "",
    verdict: str = "",
    min_score: int | None = None,
    sort: str | None = None,
    fresh: str = "",
):
    cfg = load_search_config()

    # A freshness view answers "what just landed?", so it defaults differently
    # from the ranked view: newest first, everything still open, no score
    # threshold. Scoring is a separate step that may not have run yet, and a
    # job that hasn't been scored hasn't failed the bar. Explicit query
    # parameters still win — these are only the defaults.
    is_fresh = fresh in FRESH_WINDOWS
    if status is None:
        status = PENDING if is_fresh else AppStatus.SCORED.value
    if sort is None:
        sort = "posted" if is_fresh else "score"
    if min_score is None:
        min_score = 0 if is_fresh else cfg.review.min_score
    threshold = min_score

    with session_scope() as session:
        stmt = (
            select(Job)
            .options(joinedload(Job.score))
            .join(Score, Score.job_id == Job.id, isouter=True)
        )

        if status == PENDING:
            stmt = _still_open(stmt)
        elif status and status != "all":
            try:
                stmt = stmt.where(Job.status == AppStatus(status))
            except ValueError:
                stmt = stmt.where(Job.status == AppStatus.SCORED)
        if source:
            stmt = stmt.where(Job.source == source)
        if verdict:
            stmt = stmt.where(Score.verdict == verdict)
        if threshold:
            stmt = stmt.where(Score.fit_score >= threshold)

        # Freshness is about when the employer posted, not when we scraped —
        # a job found today may have been up for a week.
        if is_fresh:
            stmt = _within(stmt, fresh)

        if sort == "posted":
            stmt = stmt.order_by(Job.posted_at.desc().nullslast())
        elif sort == "recent":
            stmt = stmt.order_by(Job.scraped_at.desc())
        elif sort == "company":
            stmt = stmt.order_by(Job.company, Score.fit_score.desc())
        else:
            stmt = stmt.order_by(Score.fit_score.desc().nullslast(), Job.scraped_at.desc())

        jobs = list(session.execute(stmt).unique().scalars())
        sources = sorted(session.execute(select(Job.source).distinct()).scalars())
        counts = _counts(session)

        # Discovered but never scored. Without this the page just looks empty
        # while dozens of jobs sit one blocked step upstream.
        unscored = session.execute(
            select(func.count(Job.id)).where(
                Job.status == AppStatus.NEW, ~Job.id.in_(select(Score.job_id))
            )
        ).scalar() or 0

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "jobs": jobs,
                "counts": counts,
                "sources": sources,
                "unscored": unscored,
                "has_api_key": setup_mod.readiness().api_key_ok,
                "route_labels": ROUTE_LABELS,
                "filters": {
                    "status": status,
                    "source": source,
                    "verdict": verdict,
                    "min_score": threshold,
                    "sort": sort,
                    "fresh": fresh,
                },
                "fresh_counts": _fresh_counts(session),
                "statuses": [s.value for s in AppStatus],
            },
        )


@app.post("/decide")
def decide(
    job_ids: list[int] = Form(default=[]),
    action: str = Form(...),
    redirect_to: str = Form(default="/"),
):
    """Approve or skip the ticked jobs.

    Approving only marks intent — `jobbot apply` still runs separately, and
    still defaults to a dry run.
    """
    if job_ids:
        with session_scope() as session:
            target = AppStatus.APPROVED if action == "approve" else AppStatus.SKIPPED
            for job in session.execute(select(Job).where(Job.id.in_(job_ids))).scalars():
                # Never move an already-submitted job back into the apply
                # queue. A stray select-all would otherwise re-send real
                # applications to employers you've already applied to.
                if job.status is AppStatus.SUBMITTED:
                    continue

                # A job on a portal we don't automate goes to the manual queue
                # rather than the apply queue, so it isn't silently dropped.
                # UNKNOWN routes are *not* diverted — the applier follows the
                # external link first and may well find Greenhouse or Lever.
                if target is AppStatus.APPROVED and job.apply_route.send_to_manual:
                    job.status = AppStatus.MANUAL
                else:
                    job.status = target
                if target is AppStatus.SKIPPED:
                    job.skip_reason = "rejected in review"
    return RedirectResponse(redirect_to, status_code=303)


@app.get("/needs-input")
def needs_input(request: Request):
    """Applications parked on a question only you can answer."""
    with session_scope() as session:
        jobs = list(
            session.execute(
                select(Job)
                .options(joinedload(Job.score), joinedload(Job.application))
                .where(Job.status == AppStatus.NEEDS_INPUT)
                .order_by(Job.id)
            )
            .unique()
            .scalars()
        )
        return templates.TemplateResponse(
            request,
            "needs_input.html",
            {"jobs": jobs, "counts": _counts(session)},
        )


@app.post("/answer")
def save_answer(
    job_id: int = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
):
    """Record an answer and un-park the application.

    The answer also enters the reusable bank, so the same question auto-fills
    on every later application.
    """
    from ..appliers.answers import remember_answer

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is not None and job.application is not None:
            application = job.application
            answers = dict(application.answers_json or {})
            answers[question] = answer
            application.answers_json = answers
            application.pending_questions = [
                q for q in (application.pending_questions or []) if q.get("question") != question
            ]
            if not application.pending_questions:
                job.status = AppStatus.APPROVED
        remember_answer(session, question, answer, approved=True)

    return RedirectResponse("/needs-input", status_code=303)


@app.get("/manual")
def manual_queue(request: Request):
    """Jobs whose portal we don't automate — apply by hand, track here."""
    with session_scope() as session:
        jobs = list(
            session.execute(
                select(Job)
                .options(joinedload(Job.score))
                .join(Score, Score.job_id == Job.id, isouter=True)
                .where(Job.status == AppStatus.MANUAL)
                .order_by(Score.fit_score.desc().nullslast())
            )
            .unique()
            .scalars()
        )
        return templates.TemplateResponse(
            request,
            "manual.html",
            {"jobs": jobs, "counts": _counts(session), "route_labels": ROUTE_LABELS},
        )


@app.get("/applications")
def applications_page(request: Request, show: str = "all"):
    """Every application attempt and how it ended.

    Approving a job only queues it. This is where you find out what actually
    happened to it — sent or not, when, and with what answers, backed by the
    screenshot taken at the moment of submission.
    """
    with session_scope() as session:
        stmt = (
            select(Job)
            .options(joinedload(Job.application), joinedload(Job.score))
            .join(Application, Application.job_id == Job.id)
            .order_by(Application.updated_at.desc())
        )
        jobs = list(session.execute(stmt).unique().scalars())

        rows = []
        for job in jobs:
            application = job.application
            sent = application.submitted_at is not None and not application.dry_run
            if show == "sent" and not sent:
                continue
            if show == "pending" and sent:
                continue
            rows.append(
                {
                    "job": job,
                    "app": application,
                    "sent": sent,
                    "state": _application_state(job, application, sent),
                    "evidence": _evidence_name(application.evidence_path),
                }
            )

        # Queued but never attempted — the state that prompts "did it work?"
        never_tried = session.execute(
            select(func.count(Job.id)).where(
                Job.status == AppStatus.APPROVED, ~Job.id.in_(select(Application.job_id))
            )
        ).scalar() or 0

        return templates.TemplateResponse(
            request,
            "applications.html",
            {
                "counts": _counts(session),
                "rows": rows,
                "show": show,
                "never_tried": never_tried,
                "sent_total": sum(1 for r in rows if r["sent"]),
            },
        )


def _application_state(job: Job, application: Application, sent: bool) -> tuple[str, str]:
    """(label, tone) describing what happened, in plain language."""
    if sent:
        return "Sent", "ok"
    if job.status is AppStatus.NEEDS_INPUT:
        return "Waiting on you", "warn"
    if job.status is AppStatus.MANUAL:
        return "Apply by hand", "warn"
    if job.status is AppStatus.FAILED:
        return "Failed", "bad"
    if application.dry_run and application.evidence_path:
        return "Dry run only — not sent", "muted"
    return "Not sent", "muted"


def _evidence_name(path: str) -> str:
    """Bare filename of an evidence screenshot, for the /evidence route."""
    return Path(path).name if path else ""


@app.get("/evidence/{filename}")
def evidence_file(filename: str):
    """Serve a screenshot from data/evidence.

    Resolved and confined to the evidence directory — the filename comes from
    a URL, so it is untrusted input and must never be able to escape.
    """
    from fastapi.responses import FileResponse, PlainTextResponse

    from ..appliers.base import EVIDENCE_DIR

    root = EVIDENCE_DIR.resolve()
    try:
        target = (root / Path(filename).name).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return PlainTextResponse("Not found", status_code=404)

    if not target.is_file() or target.suffix.lower() not in {".png", ".html"}:
        return PlainTextResponse("Not found", status_code=404)

    media = "image/png" if target.suffix.lower() == ".png" else "text/html"
    return FileResponse(target, media_type=media)


# ==========================================================================
# Setup — accounts, profile, resume
# ==========================================================================


@app.get("/setup")
def setup_page(request: Request, error: str = "", saved: str = ""):
    with session_scope() as session:
        counts = _counts(session)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "counts": counts,
            "sites": setup_mod.site_statuses(),
            "ready": setup_mod.readiness(),
            "profile": setup_mod.current_profile(),
            "running": TASKS.current,
            "error": error,
            "saved": saved,
        },
    )


@app.post("/setup/connect")
def connect_site(site: str = Form(...)):
    """Open a real browser so you can log into a job board by hand.

    No credentials pass through this application. The button launches the
    same `jobbot login` flow the CLI uses: a browser window opens, you sign in
    (2FA included), and the session cookie stays in that browser profile.
    """
    if site not in SCRAPERS:
        return RedirectResponse(f"/setup?error=Unknown+site+{site}", status_code=303)
    try:
        TASKS.start(["login", site], f"Connect {site}")
    except TaskBusy as exc:
        return RedirectResponse(f"/setup?error={_q(str(exc))}", status_code=303)
    return RedirectResponse("/run?watch=1", status_code=303)


@app.post("/setup/profile")
async def save_profile_route(request: Request):
    form = await request.form()
    payload = {key: str(value) for key, value in form.items()}
    try:
        setup_mod.save_profile(payload)
    except Exception as exc:
        return RedirectResponse(f"/setup?error={_q(str(exc))}", status_code=303)
    return RedirectResponse("/setup?saved=profile", status_code=303)


@app.post("/setup/resume")
async def upload_resume_route(resume: UploadFile = File(...)):
    data = await resume.read()
    try:
        setup_mod.save_resume(resume.filename or "", data)
    except Exception as exc:
        return RedirectResponse(f"/setup?error={_q(str(exc))}", status_code=303)
    return RedirectResponse("/setup?saved=resume", status_code=303)


# ==========================================================================
# Run — drive the pipeline
# ==========================================================================

# ==========================================================================
# Search — the shared sweep
# ==========================================================================

#: Freshness windows the Search screen offers, as hours. "any" means no window.
FRESH_CHOICES = {"1h": 1, "24h": 24, "7d": 168, "any": None}

#: Sites the Search screen can toggle, in display order.
SEARCH_SITES = ("instahyre", "cutshort", "linkedin")


def _primary_query(cfg) -> SearchQuery:
    """The query the Search form edits.

    The screen models one sweep shared across every board, so it needs a
    single query to seed from. Prefer an enabled site's first query; fall
    back to any site's, then to an empty one.
    """
    for site in SEARCH_SITES:
        conf = cfg.sites.get(site)
        if conf and conf.enabled and conf.queries:
            return conf.queries[0]
    for conf in cfg.sites.values():
        if conf.queries:
            return conf.queries[0]
    return SearchQuery(keywords="", location="")


def _fresh_key(query: SearchQuery) -> str:
    hours = query.posted_within_hours or (
        query.posted_within_days * 24 if query.posted_within_days else None
    )
    for key, value in FRESH_CHOICES.items():
        if value == hours:
            return key
    return "any"


def _extra_queries(cfg, primary: SearchQuery) -> list[dict]:
    """Queries a save from this screen would overwrite.

    The config models a query *list* per site; this screen models one shared
    sweep. Writing the form therefore replaces those lists. Anything that
    would be lost is surfaced in the UI first rather than deleted quietly —
    the LinkedIn 1h/24h pair lives here, and losing it to a form post nobody
    read would be a genuinely bad trade.
    """
    extras = []
    for site in SEARCH_SITES:
        conf = cfg.sites.get(site)
        if not conf:
            continue
        for query in conf.queries:
            # By value, not identity. After a save each site holds its own
            # equal-but-distinct copy of the shared sweep; comparing with `is`
            # counted those as losses and warned about discarding the very
            # thing that had just been written.
            if query == primary:
                continue
            extras.append(
                {
                    "site": site,
                    "label": query.label or query.keywords or "(unlabelled)",
                    "meta": query.describe()
                    if hasattr(query, "describe")
                    else f"{query.keywords} · {query.location or 'anywhere'}",
                }
            )
    return extras


@app.get("/search")
def search_page(request: Request, saved: str = ""):
    cfg = load_search_config()
    primary = _primary_query(cfg)

    with session_scope() as session:
        reachable = session.execute(
            select(func.count(Job.id))
            .join(Score, Score.job_id == Job.id, isouter=True)
            .where(Score.fit_score >= cfg.review.min_score)
        ).scalar() or 0
        unscored = session.execute(
            select(func.count(Job.id)).where(~Job.id.in_(select(Score.job_id)))
        ).scalar() or 0
        counts = _counts(session)

    enabled = [s for s in SEARCH_SITES if (c := cfg.sites.get(s)) and c.enabled]
    prefilter_rules = (
        len(cfg.prefilter.exclude_title_keywords)
        + len(cfg.prefilter.exclude_companies)
        + len(cfg.prefilter.allow_locations)
    )

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "counts": counts,
            "kw": primary.keywords,
            "loc": primary.location,
            "remote": primary.remote,
            "fresh": _fresh_key(primary),
            "fresh_choices": list(FRESH_CHOICES),
            "min_score": cfg.review.min_score,
            "excludes": cfg.prefilter.exclude_title_keywords,
            "sites": [
                {
                    "name": s,
                    "on": bool(c and c.enabled),
                    "meta": f"cap {c.daily_cap}/day" if c else "not configured",
                    "note": "terms risk — keep low"
                    if s == "linkedin"
                    else ("session valid" if c and c.enabled else "off"),
                }
                for s in SEARCH_SITES
                for c in [cfg.sites.get(s)]
            ],
            "estimates": [
                {"v": sum(cfg.sites[s].daily_cap for s in enabled), "k": "daily cap across boards"},
                {"v": prefilter_rules, "k": "prefilter rules"},
                {"v": reachable, "k": "already past the score bar"},
                {"v": f"${unscored * 0.04:.2f}", "k": "to score what's waiting"},
            ],
            "extras": _extra_queries(cfg, primary),
            "summary": (
                f"{len(enabled)} board{'' if len(enabled) == 1 else 's'} · "
                f"posted within {_fresh_key(primary)} · score ≥ {cfg.review.min_score}"
            ),
            "saved_note": saved,
        },
    )


@app.post("/search")
def save_search(
    keywords: str = Form(default=""),
    location: str = Form(default=""),
    remote: str = Form(default=""),
    fresh: str = Form(default="any"),
    min_score: int = Form(default=60),
    excludes: str = Form(default=""),
    boards: list[str] = Form(default=[]),
    then: str = Form(default=""),
):
    cfg = load_search_config()
    hours = FRESH_CHOICES.get(fresh)
    query = SearchQuery(
        keywords=keywords.strip(),
        location=location.strip(),
        remote=remote == "on",
        posted_within_hours=hours,
        label=f"Shared sweep — {fresh}" if hours else "Shared sweep",
    )

    for site in SEARCH_SITES:
        conf = cfg.sites.get(site)
        if conf is None:
            conf = SiteConfig()
            cfg.sites[site] = conf
        conf.enabled = site in boards
        conf.queries = [query]

    cfg.review.min_score = max(0, min(100, min_score))
    cfg.prefilter.exclude_title_keywords = [
        w.strip() for w in excludes.split(",") if w.strip()
    ]
    save_search_config(cfg)

    if then == "discover":
        try:
            TASKS.start(["discover"], _label("discover", "", False))
        except TaskBusy as exc:
            return RedirectResponse(f"/run?error={_q(str(exc))}", status_code=303)
        return RedirectResponse("/run?watch=1", status_code=303)
    return RedirectResponse("/search?saved=1", status_code=303)


_RUNNABLE = {"discover", "score", "apply"}


@app.get("/run")
def run_page(request: Request, error: str = "", task: str = ""):
    cfg = load_search_config()
    with session_scope() as session:
        counts = _counts(session)
        used = applications_today(session)

    # Read from config rather than restated in the template, so the panel and
    # the caps the applier actually enforces cannot drift apart.
    limits = {
        "per_day": cfg.apply.max_per_day,
        "per_company": cfg.apply.max_per_company_per_week,
        "rows": [
            {"label": "Applications today", "value": f"{used} / {cfg.apply.max_per_day}"},
            {"label": "Per company this week", "value": f"max {cfg.apply.max_per_company_per_week}"},
            {"label": "Failure circuit breaker", "value": f"{cfg.apply.failure_circuit_breaker} in a row"},
            {"label": "Evidence retention", "value": "data/evidence/"},
        ],
    }

    history = TASKS.history()
    running = TASKS.current
    # Show the running task, an explicitly requested one, or the most recent —
    # a finished run's output is the whole point of having watched it.
    shown = TASKS.get(task) if task else (running or (history[0] if history else None))

    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "counts": counts,
            "ready": setup_mod.readiness(),
            "sites": setup_mod.site_statuses(),
            "running": running,
            "shown": shown,
            "history": history,
            "limits": limits,
            "error": error,
        },
    )


@app.post("/run")
def start_run(
    command: str = Form(...),
    site: str = Form(default=""),
    limit: str = Form(default=""),
    submit: str = Form(default=""),
    confirm: str = Form(default=""),
):
    if command not in _RUNNABLE:
        return RedirectResponse(f"/run?error=Unknown+command+{command}", status_code=303)

    args = [command]
    if command == "discover" and site:
        args += ["--site", site]
    if limit.strip().isdigit():
        args += ["--limit", limit.strip()]

    if command == "apply" and submit == "on":
        # Real submissions need an explicit, separate confirmation — the
        # checkbox alone is too easy to leave ticked from a previous run.
        if confirm != "yes":
            return RedirectResponse(
                "/run?error=" + _q("Tick the confirmation box to submit real applications."),
                status_code=303,
            )
        args += ["--submit", "--yes"]

    try:
        TASKS.start(args, _label(command, site, submit == "on"))
    except TaskBusy as exc:
        return RedirectResponse(f"/run?error={_q(str(exc))}", status_code=303)
    return RedirectResponse("/run?watch=1", status_code=303)


def _label(command: str, site: str, submit: bool) -> str:
    if command == "discover":
        return f"Discover {site or 'all sites'}"
    if command == "apply":
        return "Apply (SUBMITTING)" if submit else "Apply (dry run)"
    return "Score"


@app.post("/run/stop")
def stop_run():
    task = TASKS.current
    if task is not None:
        TASKS.stop(task.id)
    return RedirectResponse("/run", status_code=303)


@app.get("/run/stream")
def run_stream(task_id: str = ""):
    """Live command output over server-sent events."""
    task = TASKS.get(task_id) if task_id else TASKS.current
    if task is None:
        return StreamingResponse(
            iter(["event: done\ndata: idle\n\n"]), media_type="text/event-stream"
        )
    return StreamingResponse(
        stream_lines(task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _q(text: str) -> str:
    from urllib.parse import quote

    return quote(text[:300])


@app.post("/mark-applied")
def mark_applied(job_id: int = Form(...), redirect_to: str = Form(default="/manual")):
    """Record that you applied by hand, so tracking stays accurate."""
    from ..db import Application

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is not None:
            job.status = AppStatus.SUBMITTED
            if job.application is None:
                session.add(
                    Application(
                        job_id=job.id, method="manual", submitted_at=utcnow(), dry_run=False
                    )
                )
            else:
                job.application.method = "manual"
                job.application.submitted_at = utcnow()
                job.application.dry_run = False
    return RedirectResponse(redirect_to, status_code=303)

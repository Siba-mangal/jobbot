"""SQLite schema and session handling.

The database is the handoff between pipeline stages, so every stage is
independently re-runnable and a crash never loses work.
"""

from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import DATA_DIR, ensure_data_dirs

DB_PATH = DATA_DIR / "jobs.db"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class ApplyRoute(enum.StrEnum):
    """How an application gets submitted for this posting."""

    BOARD_NATIVE = "board_native"  # apply on the job board itself
    ATS_GREENHOUSE = "ats_greenhouse"
    ATS_LEVER = "ats_lever"
    ATS_OTHER = "ats_other"  # recognized ATS we don't automate → manual queue
    UNKNOWN = "unknown"  # external link not yet followed → resolved at apply time

    @property
    def is_automated(self) -> bool:
        return self in {
            ApplyRoute.BOARD_NATIVE,
            ApplyRoute.ATS_GREENHOUSE,
            ApplyRoute.ATS_LEVER,
        }

    @property
    def needs_resolution(self) -> bool:
        """External link whose destination we deliberately haven't followed yet.

        Discovery does not click "Apply" — on LinkedIn that registers
        application intent, and doing it for jobs you may never apply to is
        both noisy and dishonest. So an external posting stays UNKNOWN until
        the applier follows the link, fingerprints the ATS, and either
        proceeds (Greenhouse/Lever) or files it under manual.
        """
        return self is ApplyRoute.UNKNOWN

    @property
    def send_to_manual(self) -> bool:
        """True if approving this should file it for you to do by hand."""
        return not self.is_automated and not self.needs_resolution


class Verdict(enum.StrEnum):
    STRONG = "strong"
    POSSIBLE = "possible"
    WEAK = "weak"
    DISQUALIFIED = "disqualified"


class AppStatus(enum.StrEnum):
    NEW = "new"  # scraped, not yet scored
    SCORED = "scored"  # scored, awaiting your review
    APPROVED = "approved"  # you ticked it; queued to apply
    APPLYING = "applying"  # in flight
    NEEDS_INPUT = "needs_input"  # parked on a question only you can answer
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"  # you rejected it, or prefilter dropped it
    MANUAL = "manual"  # portal not automated; apply by hand


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


class Job(Base):
    """One unique posting. Same role seen on two boards collapses to one row."""

    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "source_job_id", name="uq_job_source"),)

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(32), index=True)
    source_job_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str] = mapped_column(String(256), index=True)
    location: Mapped[str] = mapped_column(String(256), default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    salary_raw: Mapped[str] = mapped_column(String(256), default="")

    description: Mapped[str] = mapped_column(Text, default="")

    apply_route: Mapped[ApplyRoute] = mapped_column(
        Enum(ApplyRoute), default=ApplyRoute.UNKNOWN
    )
    ats_type: Mapped[str] = mapped_column(String(64), default="")
    ats_url: Mapped[str] = mapped_column(Text, default="")

    # Cross-site dedupe key: normalized company+title+location.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Other (source, id) pairs where this same role was seen.
    also_seen_on: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[AppStatus] = mapped_column(
        Enum(AppStatus), default=AppStatus.NEW, index=True
    )
    skip_reason: Mapped[str] = mapped_column(String(256), default="")

    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    score: Mapped[Score | None] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    application: Mapped[Application | None] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.id} {self.company!r} {self.title!r} [{self.source}]>"


class Score(Base):
    """LLM judgment of this job against the resume. Re-scoreable."""

    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)

    model: Mapped[str] = mapped_column(String(64))
    fit_score: Mapped[int] = mapped_column(Integer, index=True)
    verdict: Mapped[Verdict] = mapped_column(Enum(Verdict))

    strengths: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[list] = mapped_column(JSON, default=list)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    tailored_summary: Mapped[str] = mapped_column(Text, default="")

    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)

    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="score")


class Application(Base):
    """Lifecycle of one submission attempt."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True, index=True)

    method: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)

    evidence_path: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")

    # Filled answers, and any questions still awaiting your input.
    answers_json: Mapped[dict] = mapped_column(JSON, default=dict)
    pending_questions: Mapped[list] = mapped_column(JSON, default=list)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[Job] = relationship(back_populates="application")


class Answer(Base):
    """Reusable answer bank. Grows every time you approve a drafted answer."""

    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_norm: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    question_raw: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="text")  # text|number|bool|choice
    # False = Claude drafted it and you haven't signed off yet. Never submitted.
    approved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    """Audit trail; also backs daily-cap accounting and the circuit breaker."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # discover|score|apply
    site: Mapped[str] = mapped_column(String(32), default="", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")


class SiteState(Base):
    """Circuit-breaker state per site."""

    __tablename__ = "site_state"

    site: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pause_reason: Mapped[str] = mapped_column(Text, default="")
    # Rolling count of detail-page views today, for the daily cap.
    views_today: Mapped[int] = mapped_column(Integer, default=0)
    views_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^a-z0-9 ]+")
# Seniority/level noise that differs between boards for the same role.
_TITLE_NOISE = re.compile(
    r"\b(sr|snr|senior|jr|junior|lead|staff|principal|i{1,3}|iv|[1-5])\b"
)


def _norm(text: str) -> str:
    text = _NOISE.sub(" ", (text or "").lower())
    return _WS.sub(" ", text).strip()


def make_fingerprint(company: str, title: str, location: str) -> str:
    """Stable cross-site identity for a role.

    Normalizes away punctuation, casing, and seniority tokens so the same
    posting on LinkedIn and Instahyre collapses to one row. Location is
    reduced to its first component ("Bangalore, KA, India" -> "bangalore")
    because boards format it inconsistently.
    """
    company_n = _norm(company)
    title_n = _WS.sub(" ", _TITLE_NOISE.sub(" ", _norm(title))).strip()
    location_n = _norm(location.split(",")[0]) if location else ""
    key = f"{company_n}|{title_n}|{location_n}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# Engine / session
# --------------------------------------------------------------------------

_engine = None
_SessionFactory = None


def get_engine(url: str | None = None):
    global _engine, _SessionFactory
    if _engine is None or url is not None:
        ensure_data_dirs()
        _engine = create_engine(url or f"sqlite:///{DB_PATH}", future=True)

        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def init_db(url: str | None = None) -> None:
    Base.metadata.create_all(get_engine(url))


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    get_engine(url)
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

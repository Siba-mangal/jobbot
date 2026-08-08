"""Answering application form questions.

The rule this module exists to enforce:

    **The bot never invents a factual answer about you.**

Facts — years of experience, notice period, salary, work authorization — come
from ``config/profile.yaml`` or they don't get answered. If a form asks
something the profile doesn't cover, the application parks in the *Needs
input* queue rather than guessing. A wrong salary or visa answer on a real
application is far worse than a delayed one, and unlike a delay it is not
recoverable.

Opinion questions ("why do you want to work here?") may be *drafted* by
Claude, but a draft is never submitted — it is pre-filled for one-click
approval. Once approved, it enters the answer bank and auto-fills forever.

Resolution order: profile → answer bank → LLM draft (park) → park.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Profile
from ..db import Answer


class Source(StrEnum):
    PROFILE = "profile"  # a fact you stated
    BANK = "bank"  # an answer you previously approved
    DRAFT = "draft"  # Claude wrote it; awaiting your approval
    UNRESOLVED = "unresolved"  # nothing to say; park it


@dataclass
class Question:
    """A field on an application form."""

    question: str
    kind: str = "text"  # text|number|bool|choice|email|phone|url|file
    required: bool = True
    options: list[str] = field(default_factory=list)
    field_hint: str = ""  # name/id/placeholder attribute, aids matching

    def normalized(self) -> str:
        return normalize_question(self.question)


@dataclass
class Resolution:
    question: Question
    answer: str | None
    source: Source

    @property
    def can_submit(self) -> bool:
        """Only facts you stated and answers you approved may be submitted."""
        return self.answer is not None and self.source in (Source.PROFILE, Source.BANK)

    @property
    def needs_you(self) -> bool:
        return not self.can_submit


# --------------------------------------------------------------------------
# Question normalization
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")
# Words that carry no meaning for identifying a question. Deliberately does
# NOT include verbs like "describe" or qualifiers like "current"/"expected" —
# those distinguish genuinely different questions.
_FILLER = re.compile(
    r"\b(please|kindly|your|the|a|an|do you|are you|what is|what s|tell us"
    r"|enter|provide|specify|indicate|input|fill in|type|give)\b"
)


def normalize_question(text: str) -> str:
    """Stable key for the answer bank.

    Forms word the same question a dozen ways ("What is your notice period?",
    "Notice period (days)", "Please enter your notice period"). Normalizing
    lets one approved answer serve all of them.
    """
    lowered = _PUNCT.sub(" ", (text or "").lower())
    lowered = _FILLER.sub(" ", lowered)
    return _WS.sub(" ", lowered).strip()


# --------------------------------------------------------------------------
# Profile facts
# --------------------------------------------------------------------------


def _profile_facts(profile: Profile) -> list[tuple[re.Pattern, str]]:
    """(question pattern, answer) pairs drawn strictly from profile.yaml.

    Order matters — more specific patterns first. An entry whose profile value
    is blank is omitted entirely, so the question falls through to parking
    rather than being answered with an empty string.
    """
    ident, links = profile.identity, profile.links
    emp, elig = profile.employment, profile.eligibility

    candidates: list[tuple[str, str]] = [
        (r"\bfirst name\b|\bgiven name\b", ident.first_name),
        (r"\blast name\b|\bsurname\b|\bfamily name\b", ident.last_name),
        (r"\bfull name\b|^name$|\byour name\b", ident.full_name),
        (r"\bemail\b|\be-?mail address\b", ident.email),
        (r"\bphone\b|\bmobile\b|\bcontact number\b", ident.phone),
        (r"\bcurrent city\b|\bcity\b|\bwhere.*located\b|\bcurrent location\b", ident.city),
        (r"\bcountry\b", ident.country),
        (r"\blinkedin\b", links.linkedin),
        (r"\bgithub\b", links.github),
        (r"\bportfolio\b|\bpersonal website\b|\bwebsite\b", links.portfolio),
        (r"\bcurrent (company|employer)\b", emp.current_company),
        (r"\bcurrent (title|role|designation|position)\b", emp.current_title),
        (
            r"\b(total )?(years|yrs).{0,15}experience\b|\bexperience.{0,10}(years|yrs)\b",
            _fmt_number(emp.total_years_experience),
        ),
        (r"\bnotice period\b", _fmt_notice(emp.notice_period_days)),
        (r"\b(current|present).{0,10}(ctc|salary|compensation)\b", emp.current_ctc),
        (
            r"\b(expected|desired|required).{0,10}(ctc|salary|compensation)\b"
            r"|\bsalary expectation\b",
            emp.expected_ctc,
        ),
        (
            r"\b(require|need).{0,15}sponsor\w*\b|\bvisa sponsorship\b",
            _fmt_bool(elig.requires_visa_sponsorship),
        ),
        (
            r"\b(legally )?authoriz\w+ to work\b|\bwork authoriz\w+\b|\beligible to work\b",
            _fmt_authorized(elig.authorized_to_work_in),
        ),
        (r"\bwilling to relocate\b|\brelocat\w+\b", _fmt_bool(elig.willing_to_relocate)),
        (r"\bwork mode\b|\bremote or\b|\bwork preference\b", elig.preferred_work_mode),
    ]

    facts = []
    for pattern, value in candidates:
        if value:
            facts.append((re.compile(pattern, re.IGNORECASE), str(value)))

    # Free-text answers the user explicitly pre-wrote.
    for key, value in (profile.standard_answers or {}).items():
        if value:
            pattern = re.compile(re.escape(key.replace("_", " ")), re.IGNORECASE)
            facts.append((pattern, str(value)))

    return facts


def _fmt_number(value: float) -> str:
    if not value:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _fmt_notice(days: int) -> str:
    if not days:
        return ""
    if days % 30 == 0:
        months = days // 30
        return f"{months} month{'s' if months > 1 else ''}"
    return f"{days} days"


def _fmt_bool(value: bool) -> str:
    return "Yes" if value else "No"


def _fmt_authorized(countries: list[str]) -> str:
    return f"Yes, authorized to work in {', '.join(countries)}" if countries else ""


def resolve_from_profile(question: Question, profile: Profile) -> str | None:
    """Answer strictly from stated facts, or None."""
    haystack = f"{question.question} {question.field_hint}"
    for pattern, value in _profile_facts(profile):
        if pattern.search(haystack):
            return value
    return None


# --------------------------------------------------------------------------
# Answer bank
# --------------------------------------------------------------------------


def resolve_from_bank(session: Session, question: Question) -> str | None:
    """Look up a previously **approved** answer. Drafts never match here."""
    row = session.execute(
        select(Answer).where(
            Answer.question_norm == question.normalized(),
            Answer.approved.is_(True),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    row.times_used += 1
    return row.answer


def remember_answer(
    session: Session,
    question: str,
    answer: str,
    *,
    kind: str = "text",
    approved: bool = True,
) -> Answer:
    """Store an answer for reuse. Upserts on the normalized question."""
    norm = normalize_question(question)
    row = session.execute(
        select(Answer).where(Answer.question_norm == norm)
    ).scalar_one_or_none()
    if row is None:
        row = Answer(question_norm=norm, question_raw=question, answer=answer, kind=kind)
        session.add(row)
    else:
        row.answer = answer
        row.question_raw = question
    row.approved = approved
    return row


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# Questions whose answers are facts about you. Never generated, never drafted —
# if the profile doesn't have it, we park.
_FACTUAL = re.compile(
    r"\b(name|email|phone|mobile|address|city|country|salary|ctc|compensation"
    r"|notice period|visa|sponsor\w*|authoriz\w+|citizenship|clearance"
    r"|years|yrs|experience|graduat\w+|degree|university|college|gender|race"
    r"|ethnicity|veteran|disability|date of birth|dob|relocat\w+|start date"
    r"|available|linkedin|github|portfolio|website|current (company|employer|title))\b",
    re.IGNORECASE,
)

# Questions inviting an opinion. Claude may draft these; you still approve.
_OPINION = re.compile(
    r"\b(why (do you|are you|this)|what (interests|excites|motivates)"
    r"|tell us about|describe|cover letter|anything else|what makes you"
    r"|greatest (strength|weakness)|proud(est)?|challeng\w+ project)\b",
    re.IGNORECASE,
)


def is_factual(question: Question) -> bool:
    """True if answering this requires a fact, not a judgment.

    Defaults to True when a question matches neither pattern — the safe
    direction is to park an unclassifiable question rather than let a model
    write something that could be a false statement about you.
    """
    text = f"{question.question} {question.field_hint}"
    if _OPINION.search(text):
        return False
    if _FACTUAL.search(text):
        return True
    return True


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve(
    question: Question,
    profile: Profile,
    session: Session,
    *,
    draft_fn=None,
) -> Resolution:
    """Answer a single form question.

    `draft_fn(question) -> str | None` optionally supplies a Claude-written
    draft for opinion questions. Its output is *never* submittable — it comes
    back as Source.DRAFT so the caller parks the application for approval.
    """
    if value := resolve_from_profile(question, profile):
        return Resolution(question, value, Source.PROFILE)

    if value := resolve_from_bank(session, question):
        return Resolution(question, value, Source.BANK)

    if not is_factual(question) and draft_fn is not None:
        if draft := draft_fn(question):
            return Resolution(question, draft, Source.DRAFT)

    return Resolution(question, None, Source.UNRESOLVED)


def resolve_all(
    questions: list[Question],
    profile: Profile,
    session: Session,
    *,
    draft_fn=None,
) -> tuple[dict[str, str], list[dict]]:
    """Resolve a whole form.

    Returns (answers ready to submit, questions needing you). A non-empty
    second element means the application must park — partial submission of a
    form is not an option.
    """
    ready: dict[str, str] = {}
    pending: list[dict] = []

    for question in questions:
        resolution = resolve(question, profile, session, draft_fn=draft_fn)
        if resolution.can_submit:
            ready[question.question] = resolution.answer
            continue
        if not question.required and resolution.source is Source.UNRESOLVED:
            continue  # optional and nothing to say — leave it blank
        pending.append(
            {
                "question": question.question,
                "kind": question.kind,
                "required": question.required,
                "options": question.options,
                "draft": resolution.answer if resolution.source is Source.DRAFT else "",
            }
        )

    return ready, pending

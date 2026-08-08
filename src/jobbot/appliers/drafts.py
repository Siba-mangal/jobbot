"""Drafting answers to opinion questions.

Only ever reached for questions classified as non-factual — "why this
company", "describe a project you're proud of". Facts never come near this
module.

A draft is not an answer. It comes back tagged as a draft, parks the
application, and appears in the *Needs input* tab pre-filled for you to edit
and approve. Once approved it enters the answer bank and auto-fills on every
later application.
"""

from __future__ import annotations

import anthropic

from ..config import Profile
from ..db import Job
from .answers import Question

MAX_TOKENS = 2_000

_SYSTEM = """\
You are drafting one answer to one question on a job application, on behalf \
of the candidate whose resume is below. The draft will be shown to the \
candidate for editing before it is ever submitted.

Rules:
  - Every claim must be supported by the resume. Do not invent employers, \
technologies, dates, metrics, or achievements. If the resume does not \
evidence something, do not say it.
  - Write in the candidate's first person, plainly. No greeting, no \
sign-off, no placeholders like [Company] or [X years].
  - Match the length to the question. A short-answer box wants 2-4 sentences; \
a cover-letter field wants a short paragraph. Never pad.
  - Be specific to this posting. A sentence that would fit any job at any \
company is wasted.
  - Output only the answer text — no preamble, no quotation marks, no \
commentary about the task.\
"""


class Drafter:
    """Callable that drafts one answer, bound to a job and profile."""

    def __init__(
        self,
        job: Job,
        profile: Profile,
        resume_text: str,
        *,
        model: str = "claude-opus-5",
        effort: str = "medium",
        client: anthropic.Anthropic | None = None,
        tailored_summary: str = "",
    ):
        self.job = job
        self.profile = profile
        self.resume_text = resume_text
        self.model = model
        self.effort = effort
        self.tailored_summary = tailored_summary
        self.client = client or anthropic.Anthropic()
        self.errors: list[str] = []

    def _system_blocks(self) -> list[dict]:
        # Same caching logic as scoring: resume is stable across every
        # question and every job, so it goes in the cached prefix.
        return [
            {"type": "text", "text": _SYSTEM},
            {
                "type": "text",
                "text": f"<resume>\n{self.resume_text}\n</resume>",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ]

    def _user_message(self, question: Question) -> str:
        context = ""
        if self.tailored_summary:
            context = f"<why_this_candidate_fits>\n{self.tailored_summary}\n</why_this_candidate_fits>\n\n"
        return (
            f"<job>\n"
            f"<title>{self.job.title}</title>\n"
            f"<company>{self.job.company}</company>\n"
            f"<description>\n{self.job.description[:12_000]}\n</description>\n"
            f"</job>\n\n"
            f"{context}"
            f"<question>{question.question}</question>\n\n"
            f"Draft the candidate's answer to that question."
        )

    def __call__(self, question: Question) -> str | None:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self._system_blocks(),
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": self._user_message(question)}],
            )
        except anthropic.APIError as exc:
            self.errors.append(f"draft failed for {question.question!r}: {exc}")
            return None

        if response.stop_reason in ("refusal", "max_tokens"):
            self.errors.append(
                f"draft {response.stop_reason} for {question.question!r}"
            )
            return None

        text = "".join(block.text for block in response.content if block.type == "text").strip()
        return text or None

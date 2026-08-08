"""The structured output shape for a scored job.

Constraints worth knowing: structured outputs require every field to be
required and `additionalProperties: false`, so no Optionals and
`extra="forbid"`. Numeric bounds (`ge`/`le`) are *not* part of the supported
JSON Schema subset — the score is clamped in a validator instead, which also
means an out-of-range score degrades gracefully rather than throwing away a
whole API call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ..db import Verdict


class FitScore(BaseModel):
    """Claude's judgment of one job against the resume."""

    model_config = ConfigDict(extra="forbid")

    fit_score: int
    """0-100. How well this candidate matches this specific role."""

    verdict: Literal["strong", "possible", "weak", "disqualified"]
    """Bucket for the score. `disqualified` means a hard blocker exists."""

    strengths: list[str]
    """Concrete resume evidence matching the JD's requirements."""

    gaps: list[str]
    """JD requirements with no supporting evidence in the resume."""

    blockers: list[str]
    """Hard disqualifiers: visa, location, clearance, years far below the bar."""

    tailored_summary: str
    """2-3 sentences pitching this candidate for this role, usable in a
    cover-letter field. Drawn only from the resume."""

    @field_validator("fit_score")
    @classmethod
    def _clamp(cls, value: int) -> int:
        return max(0, min(100, value))

    @field_validator("strengths", "gaps", "blockers")
    @classmethod
    def _tidy(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    def to_verdict(self) -> Verdict:
        return Verdict(self.verdict)

    @property
    def is_disqualified(self) -> bool:
        return self.verdict == "disqualified" or bool(self.blockers)

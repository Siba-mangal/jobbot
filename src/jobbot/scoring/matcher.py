"""Scoring jobs against the resume.

Two paths, same prompt:

- **live**  — one request per job. Immediate, good for small batches.
- **batch** — the Message Batches API at 50% price for larger queues. Scoring
  isn't latency-sensitive, so this is close to free savings.

Both rely on the resume being a cached prompt prefix (see prompts.py). Cache
health is recorded per score row and surfaced by ``jobbot score`` — if
cache reads are zero across a run, something is invalidating the prefix and
you are paying full price without knowing it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import anthropic
from pydantic import ValidationError

from ..db import Job, Score
from .prompts import system_blocks, user_message
from .schema import FitScore

MAX_TOKENS = 8_000
"""Caps thinking *plus* output. Opus 5 thinks by default, so a tight budget
truncates the JSON mid-object."""


class ScoringError(RuntimeError):
    pass


@dataclass
class ScoreResult:
    job_id: int
    score: FitScore | None = None
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.score is not None


@dataclass
class ScoringStats:
    scored: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    used_batch: bool = False

    def add(self, result: ScoreResult) -> None:
        if result.ok:
            self.scored += 1
        else:
            self.failed += 1
            if result.error:
                self.errors.append(result.error)
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cache_read_tokens += result.cache_read_tokens
        self.cache_write_tokens += result.cache_write_tokens

    @property
    def cache_hit_ratio(self) -> float:
        """Share of prefix tokens served from cache. Near 1.0 after the first
        call is healthy; 0.0 across a whole run means the prefix is unstable."""
        total = self.cache_read_tokens + self.cache_write_tokens
        return self.cache_read_tokens / total if total else 0.0

    def estimated_cost_usd(self, model: str, provider: str = "anthropic") -> float:
        """Rough list-price estimate. Input/output rates per million tokens.

        A model running on your own machine costs nothing per token, so a
        local provider reports 0 rather than billing you at Claude's rates
        for a llama running on your laptop.
        """
        if provider in ("ollama", "lmstudio", "custom"):
            return 0.0
        rates = {
            "claude-opus-5": (5.0, 25.0),
            "claude-opus-4-8": (5.0, 25.0),
            "claude-sonnet-5": (3.0, 15.0),
            "claude-haiku-4-5": (1.0, 5.0),
            "gpt-5": (1.25, 10.0),
            "gpt-5-mini": (0.25, 2.0),
            "gpt-4o": (2.5, 10.0),
            "gpt-4o-mini": (0.15, 0.6),
            "gemini-2.5-pro": (1.25, 10.0),
            "gemini-2.5-flash": (0.3, 2.5),
        }
        # Unknown hosted model: fall back to the configured provider's usual
        # flagship rate rather than pretending it is free.
        default = (5.0, 25.0) if provider == "anthropic" else (1.25, 10.0)
        rate_in, rate_out = rates.get(model, default)
        cost = (
            self.input_tokens * rate_in
            + self.cache_write_tokens * rate_in * 1.25
            + self.cache_read_tokens * rate_in * 0.1
            + self.output_tokens * rate_out
        ) / 1_000_000
        return cost * 0.5 if self.used_batch else cost

    def as_dict(self) -> dict:
        return {
            "scored": self.scored,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_ratio": round(self.cache_hit_ratio, 3),
            "used_batch": self.used_batch,
            "errors": self.errors[:10],
        }


# --------------------------------------------------------------------------


class Matcher:
    #: Only the native Claude path has a half-price batch endpoint.
    supports_batch = True

    def __init__(
        self,
        resume_text: str,
        *,
        model: str = "claude-opus-5",
        effort: str = "high",
        client: anthropic.Anthropic | None = None,
    ):
        if not resume_text.strip():
            raise ScoringError("Resume text is empty — nothing to score against.")
        self.resume_text = resume_text
        self.model = model
        self.effort = effort
        self.client = client or anthropic.Anthropic()
        self._system = system_blocks(resume_text)

    # ------------------------------------------------------------------
    # Live path
    # ------------------------------------------------------------------

    def score_one(self, job: Job) -> ScoreResult:
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self._system,
                output_config={"effort": self.effort},
                output_format=FitScore,
                messages=[
                    {
                        "role": "user",
                        "content": user_message(
                            title=job.title,
                            company=job.company,
                            location=job.location,
                            description=job.description,
                        ),
                    }
                ],
            )
        except anthropic.APIStatusError as exc:
            return ScoreResult(job_id=job.id, error=f"API {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            return ScoreResult(job_id=job.id, error=f"connection error: {exc}")

        usage = response.usage
        result = ScoreResult(
            job_id=job.id,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        )

        if response.stop_reason == "refusal":
            result.error = "model refused to score this posting"
            return result
        if response.stop_reason == "max_tokens":
            result.error = f"response truncated at max_tokens ({MAX_TOKENS})"
            return result

        parsed = response.parsed_output
        if parsed is None:
            result.error = "no structured output returned"
            return result

        result.score = parsed
        return result

    def score_many(self, jobs: list[Job], *, on_event=None) -> list[ScoreResult]:
        emit = on_event or (lambda msg: None)
        results = []
        for i, job in enumerate(jobs, 1):
            result = self.score_one(job)
            results.append(result)
            if result.ok:
                emit(f"  [{i}/{len(jobs)}] {result.score.fit_score:3d}  {job.title} @ {job.company}")
            else:
                emit(f"  [{i}/{len(jobs)}] ERR  {job.title} @ {job.company}: {result.error}")
        return results

    # ------------------------------------------------------------------
    # Batch path
    # ------------------------------------------------------------------

    def score_batch(
        self,
        jobs: list[Job],
        *,
        poll_seconds: int = 20,
        timeout_seconds: int = 24 * 3600,
        on_event=None,
    ) -> list[ScoreResult]:
        """Score via the Batches API at 50% price.

        `custom_id` carries the job id. Results come back in arbitrary order,
        so they are keyed by custom_id, never by position.
        """
        emit = on_event or (lambda msg: None)

        schema = FitScore.model_json_schema()
        requests = [
            {
                "custom_id": f"job-{job.id}",
                "params": {
                    "model": self.model,
                    "max_tokens": MAX_TOKENS,
                    "system": self._system,
                    "output_config": {
                        "effort": self.effort,
                        "format": {"type": "json_schema", "schema": schema},
                    },
                    "messages": [
                        {
                            "role": "user",
                            "content": user_message(
                                title=job.title,
                                company=job.company,
                                location=job.location,
                                description=job.description,
                            ),
                        }
                    ],
                },
            }
            for job in jobs
        ]

        batch = self.client.messages.batches.create(requests=requests)
        emit(f"Batch {batch.id} submitted with {len(requests)} jobs. Polling…")

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            batch = self.client.messages.batches.retrieve(batch.id)
            if batch.processing_status == "ended":
                break
            counts = batch.request_counts
            emit(
                f"  {batch.processing_status}: "
                f"{counts.succeeded} done, {counts.processing} in flight, "
                f"{counts.errored} errored"
            )
            time.sleep(poll_seconds)
        else:
            raise ScoringError(f"Batch {batch.id} did not finish within the timeout.")

        by_id = {job.id: job for job in jobs}
        results: list[ScoreResult] = []

        for entry in self.client.messages.batches.results(batch.id):
            job_id = int(entry.custom_id.removeprefix("job-"))
            if job_id not in by_id:
                continue
            results.append(_result_from_batch_entry(job_id, entry))

        # Anything the API never returned still needs a row in the results.
        returned = {r.job_id for r in results}
        for job_id in by_id:
            if job_id not in returned:
                results.append(ScoreResult(job_id=job_id, error="no result returned by batch"))

        return results


def _result_from_batch_entry(job_id: int, entry) -> ScoreResult:
    outcome = entry.result
    if outcome.type != "succeeded":
        detail = getattr(getattr(outcome, "error", None), "type", outcome.type)
        return ScoreResult(job_id=job_id, error=f"batch {outcome.type}: {detail}")

    message = outcome.message
    usage = message.usage
    result = ScoreResult(
        job_id=job_id,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
    )

    if message.stop_reason == "refusal":
        result.error = "model refused to score this posting"
        return result
    if message.stop_reason == "max_tokens":
        result.error = f"response truncated at max_tokens ({MAX_TOKENS})"
        return result

    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text:
        result.error = "empty response"
        return result

    try:
        result.score = FitScore.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        result.error = f"could not parse response: {exc}"
    return result


def to_score_row(result: ScoreResult, model: str) -> Score:
    """Convert a successful result into a DB row."""
    assert result.score is not None
    fit = result.score
    return Score(
        job_id=result.job_id,
        model=model,
        fit_score=fit.fit_score,
        verdict=fit.to_verdict(),
        strengths=fit.strengths,
        gaps=fit.gaps,
        blockers=fit.blockers,
        tailored_summary=fit.tailored_summary,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
    )

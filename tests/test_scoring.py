"""Scoring tests. No network — the Anthropic client is stubbed, so these
verify request *shape* (especially prompt-cache placement) and result parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from helpers import RESUME, VALID, make_job, usage
from pydantic import ValidationError

from jobbot.db import Verdict
from jobbot.scoring.matcher import Matcher, ScoringError, ScoringStats, to_score_row
from jobbot.scoring.prompts import RUBRIC, system_blocks, user_message
from jobbot.scoring.schema import FitScore


class FakeMessages:
    """Records the kwargs it was called with, so tests can assert on shape."""

    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def ok_response(parsed=None, **overrides):
    return SimpleNamespace(
        parsed_output=parsed if parsed is not None else FitScore.model_validate(VALID),
        stop_reason=overrides.pop("stop_reason", "end_turn"),
        usage=overrides.pop("usage", usage()),
        content=overrides.pop("content", []),
    )


# --------------------------------------------------------------------------
# Prompt construction — the cache-critical part
# --------------------------------------------------------------------------


class TestSystemBlocks:
    def test_rubric_comes_first_then_resume(self):
        blocks = system_blocks(RESUME)
        assert blocks[0]["text"] == RUBRIC
        assert RESUME in blocks[1]["text"]

    def test_cache_breakpoint_is_on_the_last_block_only(self):
        # Caching is a prefix match and renders tools -> system -> messages.
        # The breakpoint must sit on the final system block so the whole
        # rubric+resume prefix is cached together.
        blocks = system_blocks(RESUME)
        assert "cache_control" not in blocks[0]
        assert blocks[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_cache_can_be_disabled(self):
        assert all("cache_control" not in b for b in system_blocks(RESUME, cache=False))

    def test_blocks_are_byte_identical_across_calls(self):
        # Any instability here silently destroys the cache hit rate.
        assert system_blocks(RESUME) == system_blocks(RESUME)

    def test_prefix_contains_nothing_job_specific(self):
        blocks = system_blocks(RESUME)
        rendered = json.dumps(blocks)
        for leak in ("Acme", "Backend Engineer", "instahyre", "http"):
            assert leak not in rendered, f"{leak!r} leaked into the cached prefix"


class TestUserMessage:
    def test_includes_the_job_fields(self):
        msg = user_message(
            title="Backend Engineer", company="Acme", location="Pune", description="Build things."
        )
        assert "Backend Engineer" in msg
        assert "Acme" in msg
        assert "Pune" in msg
        assert "Build things." in msg

    def test_blank_location_is_labelled(self):
        msg = user_message(title="T", company="C", location="", description="D")
        assert "not stated" in msg

    def test_long_description_is_truncated_keeping_head_and_tail(self):
        body = "HEAD_MARKER " + ("filler " * 20_000) + " TAIL_MARKER"
        msg = user_message(title="T", company="C", location="L", description=body, max_chars=2_000)
        assert "HEAD_MARKER" in msg
        assert "TAIL_MARKER" in msg  # requirements often sit at the end
        assert "truncated" in msg
        assert len(msg) < 4_000

    def test_short_description_is_untouched(self):
        msg = user_message(title="T", company="C", location="L", description="Short JD")
        assert "truncated" not in msg


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


class TestFitScore:
    def test_parses_a_valid_payload(self):
        score = FitScore.model_validate(VALID)
        assert score.fit_score == 82
        assert score.to_verdict() is Verdict.POSSIBLE

    def test_out_of_range_score_is_clamped_not_rejected(self):
        # Losing a whole paid API call over a 101 would be silly.
        assert FitScore.model_validate({**VALID, "fit_score": 150}).fit_score == 100
        assert FitScore.model_validate({**VALID, "fit_score": -5}).fit_score == 0

    def test_blank_list_entries_are_dropped(self):
        score = FitScore.model_validate({**VALID, "gaps": ["real gap", "", "  "]})
        assert score.gaps == ["real gap"]

    def test_blockers_imply_disqualified(self):
        score = FitScore.model_validate({**VALID, "blockers": ["needs US work authorization"]})
        assert score.is_disqualified

    def test_clean_score_is_not_disqualified(self):
        assert not FitScore.model_validate(VALID).is_disqualified

    def test_unknown_verdict_is_rejected(self):
        with pytest.raises(ValidationError):
            FitScore.model_validate({**VALID, "verdict": "maybe"})

    def test_extra_fields_are_rejected(self):
        # Structured outputs need additionalProperties:false; this asserts the
        # generated schema will carry it.
        with pytest.raises(ValidationError):
            FitScore.model_validate({**VALID, "surprise": 1})

    def test_json_schema_is_structured_output_compatible(self):
        schema = FitScore.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(VALID)


# --------------------------------------------------------------------------
# Live scoring path
# --------------------------------------------------------------------------


class TestScoreOne:
    def test_happy_path(self):
        matcher = Matcher(RESUME, client=FakeClient(ok_response()))
        result = matcher.score_one(make_job())
        assert result.ok
        assert result.score.fit_score == 82

    def test_sends_the_cached_system_prefix(self):
        client = FakeClient(ok_response())
        Matcher(RESUME, client=client).score_one(make_job())
        system = client.messages.calls[0]["system"]
        assert system[-1]["cache_control"]["type"] == "ephemeral"

    def test_passes_model_and_effort_through(self):
        client = FakeClient(ok_response())
        Matcher(RESUME, model="claude-sonnet-5", effort="medium", client=client).score_one(make_job())
        call = client.messages.calls[0]
        assert call["model"] == "claude-sonnet-5"
        assert call["output_config"]["effort"] == "medium"

    def test_records_token_usage(self):
        response = ok_response(
            usage=usage(input_tokens=900, output_tokens=400, cache_read_input_tokens=1200)
        )
        result = Matcher(RESUME, client=FakeClient(response)).score_one(make_job())
        assert result.input_tokens == 900
        assert result.output_tokens == 400
        assert result.cache_read_tokens == 1200

    def test_refusal_is_an_error_not_a_crash(self):
        result = Matcher(RESUME, client=FakeClient(ok_response(stop_reason="refusal"))).score_one(
            make_job()
        )
        assert not result.ok
        assert "refus" in result.error

    def test_truncation_is_reported_not_silently_accepted(self):
        # A truncated response can still carry a partial parsed_output; treating
        # it as a real score would persist garbage.
        result = Matcher(RESUME, client=FakeClient(ok_response(stop_reason="max_tokens"))).score_one(
            make_job()
        )
        assert not result.ok
        assert "truncated" in result.error

    def test_missing_structured_output_is_an_error(self):
        response = SimpleNamespace(
            parsed_output=None, stop_reason="end_turn", usage=usage(), content=[]
        )
        result = Matcher(RESUME, client=FakeClient(response)).score_one(make_job())
        assert not result.ok

    def test_empty_resume_is_rejected_upfront(self):
        with pytest.raises(ScoringError, match="empty"):
            Matcher("   ", client=FakeClient(ok_response()))

    def test_one_bad_job_does_not_stop_the_rest(self):
        class Flaky(FakeMessages):
            def parse(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 2:
                    return ok_response(stop_reason="refusal")
                return ok_response()

        client = FakeClient(None)
        client.messages = Flaky(None)
        results = Matcher(RESUME, client=client).score_many(
            [make_job(1), make_job(2), make_job(3)]
        )
        assert [r.ok for r in results] == [True, False, True]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


class TestStats:
    def test_counts_successes_and_failures(self):
        from jobbot.scoring.matcher import ScoreResult

        stats = ScoringStats()
        stats.add(ScoreResult(job_id=1, score=FitScore.model_validate(VALID)))
        stats.add(ScoreResult(job_id=2, error="boom"))
        assert (stats.scored, stats.failed) == (1, 1)
        assert stats.errors == ["boom"]

    def test_cache_hit_ratio(self):
        stats = ScoringStats(cache_read_tokens=900, cache_write_tokens=100)
        assert stats.cache_hit_ratio == pytest.approx(0.9)

    def test_cache_ratio_is_zero_when_nothing_cached(self):
        assert ScoringStats().cache_hit_ratio == 0.0

    def test_batch_halves_the_cost_estimate(self):
        live = ScoringStats(input_tokens=1_000_000, output_tokens=0)
        batched = ScoringStats(input_tokens=1_000_000, output_tokens=0, used_batch=True)
        assert batched.estimated_cost_usd("claude-opus-5") == pytest.approx(
            live.estimated_cost_usd("claude-opus-5") / 2
        )

    def test_cache_reads_are_cheap(self):
        cached = ScoringStats(cache_read_tokens=1_000_000)
        uncached = ScoringStats(input_tokens=1_000_000)
        assert cached.estimated_cost_usd("claude-opus-5") < uncached.estimated_cost_usd(
            "claude-opus-5"
        ) / 5


class TestToScoreRow:
    def test_maps_every_field(self):
        from jobbot.scoring.matcher import ScoreResult

        result = ScoreResult(
            job_id=7, score=FitScore.model_validate(VALID), input_tokens=10, output_tokens=20
        )
        row = to_score_row(result, "claude-opus-5")
        assert row.job_id == 7
        assert row.fit_score == 82
        assert row.verdict is Verdict.POSSIBLE
        assert row.strengths == VALID["strengths"]
        assert row.model == "claude-opus-5"

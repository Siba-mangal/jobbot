"""Batch-path tests.

The trap this guards: batch results come back in arbitrary order, so they must
be keyed by `custom_id`. Zipping them against the input list would silently
attach the wrong score to the wrong job — a bug that never crashes and
corrupts every downstream decision.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from helpers import RESUME, VALID, make_job, usage

from jobbot.scoring.matcher import Matcher, ScoringError
from jobbot.scoring.schema import FitScore


def text_message(payload: dict, *, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason=stop_reason,
        usage=usage(),
    )


def succeeded(job_id: int, payload: dict, *, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        custom_id=f"job-{job_id}",
        result=SimpleNamespace(type="succeeded", message=text_message(payload, stop_reason=stop_reason)),
    )


def errored(job_id: int, error_type: str = "invalid_request"):
    return SimpleNamespace(
        custom_id=f"job-{job_id}",
        result=SimpleNamespace(type="errored", error=SimpleNamespace(type=error_type)),
    )


class FakeBatches:
    def __init__(self, results):
        self._results = results
        self.created_requests = None

    def create(self, requests):
        self.created_requests = requests
        return SimpleNamespace(id="batch_1", processing_status="in_progress")

    def retrieve(self, _batch_id):
        return SimpleNamespace(
            id="batch_1",
            processing_status="ended",
            request_counts=SimpleNamespace(succeeded=len(self._results), processing=0, errored=0),
        )

    def results(self, _batch_id):
        return iter(self._results)


class FakeClient:
    def __init__(self, results):
        self.messages = SimpleNamespace(batches=FakeBatches(results))


class TestBatchRequestShape:
    def test_custom_id_carries_the_job_id(self):
        client = FakeClient([succeeded(1, VALID), succeeded(2, VALID)])
        Matcher(RESUME, client=client).score_batch([make_job(1), make_job(2)], poll_seconds=0)
        ids = [r["custom_id"] for r in client.messages.batches.created_requests]
        assert ids == ["job-1", "job-2"]

    def test_shares_the_cached_system_prefix(self):
        client = FakeClient([succeeded(1, VALID)])
        Matcher(RESUME, client=client).score_batch([make_job(1)], poll_seconds=0)
        params = client.messages.batches.created_requests[0]["params"]
        assert params["system"][-1]["cache_control"]["type"] == "ephemeral"

    def test_requests_a_json_schema(self):
        client = FakeClient([succeeded(1, VALID)])
        Matcher(RESUME, client=client).score_batch([make_job(1)], poll_seconds=0)
        fmt = client.messages.batches.created_requests[0]["params"]["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["additionalProperties"] is False


class TestBatchResults:
    def test_results_are_keyed_by_custom_id_not_position(self):
        # Deliberately return them backwards.
        high = {**VALID, "fit_score": 95}
        low = {**VALID, "fit_score": 20}
        client = FakeClient([succeeded(2, low), succeeded(1, high)])

        results = Matcher(RESUME, client=client).score_batch(
            [make_job(1), make_job(2)], poll_seconds=0
        )
        by_id = {r.job_id: r for r in results}
        assert by_id[1].score.fit_score == 95
        assert by_id[2].score.fit_score == 20

    def test_errored_entry_becomes_a_failed_result(self):
        client = FakeClient([succeeded(1, VALID), errored(2)])
        results = Matcher(RESUME, client=client).score_batch(
            [make_job(1), make_job(2)], poll_seconds=0
        )
        by_id = {r.job_id: r for r in results}
        assert by_id[1].ok
        assert not by_id[2].ok
        assert "invalid_request" in by_id[2].error

    def test_missing_result_still_produces_a_row(self):
        # If a job silently never comes back, it must not vanish — otherwise it
        # sits in NEW forever and gets re-submitted on every run.
        client = FakeClient([succeeded(1, VALID)])
        results = Matcher(RESUME, client=client).score_batch(
            [make_job(1), make_job(2)], poll_seconds=0
        )
        by_id = {r.job_id: r for r in results}
        assert set(by_id) == {1, 2}
        assert not by_id[2].ok

    def test_truncated_response_is_rejected(self):
        client = FakeClient([succeeded(1, VALID, stop_reason="max_tokens")])
        results = Matcher(RESUME, client=client).score_batch([make_job(1)], poll_seconds=0)
        assert not results[0].ok
        assert "truncated" in results[0].error

    def test_unparseable_json_is_an_error_not_a_crash(self):
        broken = SimpleNamespace(
            custom_id="job-1",
            result=SimpleNamespace(
                type="succeeded",
                message=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="not json at all")],
                    stop_reason="end_turn",
                    usage=usage(),
                ),
            ),
        )
        results = Matcher(RESUME, client=FakeClient([broken])).score_batch(
            [make_job(1)], poll_seconds=0
        )
        assert not results[0].ok
        assert "parse" in results[0].error

    def test_schema_violation_is_an_error(self):
        client = FakeClient([succeeded(1, {"fit_score": 50})])  # missing required fields
        results = Matcher(RESUME, client=client).score_batch([make_job(1)], poll_seconds=0)
        assert not results[0].ok

    def test_unknown_custom_id_is_ignored(self):
        client = FakeClient([succeeded(1, VALID), succeeded(999, VALID)])
        results = Matcher(RESUME, client=client).score_batch([make_job(1)], poll_seconds=0)
        assert [r.job_id for r in results] == [1]


class TestBatchTimeout:
    def test_never_ending_batch_raises(self):
        class NeverEnds(FakeBatches):
            def retrieve(self, _batch_id):
                return SimpleNamespace(
                    id="batch_1",
                    processing_status="in_progress",
                    request_counts=SimpleNamespace(succeeded=0, processing=1, errored=0),
                )

        client = FakeClient([])
        client.messages.batches = NeverEnds([])
        with pytest.raises(ScoringError, match="did not finish"):
            Matcher(RESUME, client=client).score_batch(
                [make_job(1)], poll_seconds=0, timeout_seconds=0
            )


def test_fitscore_roundtrips_through_json():
    payload = FitScore.model_validate(VALID).model_dump()
    assert FitScore.model_validate(json.loads(json.dumps(payload))) == FitScore.model_validate(VALID)

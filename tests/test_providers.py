"""Scoring through OpenAI, Gemini, and locally-hosted models.

One client covers all of them because they all speak OpenAI's
`/v1/chat/completions` shape. What differs — and what these tests pin — is
how much of the structured-output contract each one honours:

- OpenAI and recent Gemini enforce a JSON schema.
- Ollama and LM Studio accept `json_object` but not always a schema.
- Some gateways reject an unknown `response_format` with a flat 400.

So the client walks down a ladder and remembers where it landed, and the
result is validated against `FitScore` no matter which rung answered. "The
endpoint claims JSON mode" and "the bytes parse" are different claims.

No test here touches the network — httpx is driven by a MockTransport.
"""

from __future__ import annotations

import json

import httpx
import pytest
from helpers import make_job

from jobbot.config import ModelConfig
from jobbot.scoring.providers import (
    _OPTIONAL_EXTRAS,
    OpenAICompatScorer,
    _extract_json,
    build_scorer,
)

VALID_FIT = {
    "fit_score": 82,
    "verdict": "possible",
    "strengths": ["6 years of Python"],
    "gaps": ["no Kubernetes"],
    "blockers": [],
    "tailored_summary": "Six years building Python services.",
}

RESUME = "Jane Doe. Backend engineer. Python, Go, PostgreSQL, Kafka. 6 years." * 5


def completion(content: str, **usage) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, **usage},
    }


def scorer_for(handler, **kwargs) -> OpenAICompatScorer:
    transport = httpx.MockTransport(handler)
    return OpenAICompatScorer(
        RESUME,
        model=kwargs.pop("model", "gpt-5"),
        base_url=kwargs.pop("base_url", "https://api.openai.com/v1"),
        api_key=kwargs.pop("api_key", "sk-test"),
        client=httpx.Client(transport=transport),
    )


# ----------------------------------------------------------------------
# The happy path


def test_scores_a_job_from_a_plain_completion():
    scorer = scorer_for(lambda r: httpx.Response(200, json=completion(json.dumps(VALID_FIT))))
    result = scorer.score_one(make_job())
    assert result.ok
    assert result.score.fit_score == 82
    assert result.input_tokens == 100 and result.output_tokens == 50


def test_the_request_is_openai_shaped():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler).score_one(make_job())
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["model"] == "gpt-5"
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    # Scores must not swing between runs on the same posting.
    assert seen["temperature"] == 0


def test_the_resume_goes_in_the_system_message_not_the_job():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler).score_one(make_job())
    system, user = seen["messages"]
    assert "Jane Doe" in system["content"]
    assert "Jane Doe" not in user["content"]
    assert "backend engineer" in user["content"].lower()


def test_a_local_endpoint_sends_no_authorization_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler, base_url="http://localhost:11434/v1", api_key=None).score_one(make_job())
    assert seen["auth"] is None


# ----------------------------------------------------------------------
# The response-format ladder


def test_json_schema_is_tried_first():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler).score_one(make_job())
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["strict"] is True


def test_falls_back_to_json_object_when_schema_is_rejected():
    modes = []

    def handler(request):
        body = json.loads(request.content)
        mode = (body.get("response_format") or {}).get("type", "prompt")
        modes.append(mode)
        if mode == "json_schema":
            return httpx.Response(400, text="unsupported response_format: json_schema")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    result = scorer_for(handler).score_one(make_job())
    assert result.ok
    assert modes == ["json_schema", "json_object"]


def test_falls_all_the_way_to_prompting():
    modes = []

    def handler(request):
        body = json.loads(request.content)
        mode = (body.get("response_format") or {}).get("type", "prompt")
        modes.append(mode)
        if mode != "prompt":
            return httpx.Response(400, text="response_format is not supported")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    result = scorer_for(handler).score_one(make_job())
    assert result.ok
    assert modes == ["json_schema", "json_object", "prompt"]


def test_the_schema_is_put_in_the_prompt_on_the_last_rung():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("response_format"):
            return httpx.Response(400, text="response_format unsupported")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler).score_one(make_job())
    assert "fit_score" in bodies[-1]["messages"][0]["content"]


def test_a_rejected_mode_is_not_retried_for_the_next_job():
    """Otherwise every job pays two wasted round trips to relearn the same
    thing — which on a slow local server is most of the run."""
    modes = []

    def handler(request):
        mode = (json.loads(request.content).get("response_format") or {}).get("type", "prompt")
        modes.append(mode)
        if mode == "json_schema":
            return httpx.Response(400, text="bad response_format")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer = scorer_for(handler)
    scorer.score_one(make_job(1))
    scorer.score_one(make_job(2))
    assert modes == ["json_schema", "json_object", "json_object"]


def test_a_400_unrelated_to_response_format_is_not_downgraded():
    """A bad model name is a real error, not a format negotiation."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="model 'nope' not found")

    result = scorer_for(handler, model="nope").score_one(make_job())
    assert not result.ok
    assert "not found" in result.error
    assert len(calls) == 1


# ----------------------------------------------------------------------
# Parsing what actually comes back


@pytest.mark.parametrize(
    "wrapper",
    [
        "{body}",
        "```json\n{body}\n```",
        "```\n{body}\n```",
        "Here is the assessment:\n{body}",
        "{body}\n\nHope that helps!",
    ],
)
def test_json_is_recovered_from_prose_and_fences(wrapper):
    """Models that cannot be held to a schema still emit the right object —
    just wrapped. Failing on a code fence would throw away a good score."""
    content = wrapper.format(body=json.dumps(VALID_FIT))
    scorer = scorer_for(lambda r: httpx.Response(200, json=completion(content)))
    assert scorer.score_one(make_job()).score.fit_score == 82


def test_extract_json_rejects_a_response_with_no_object():
    with pytest.raises(ValueError):
        _extract_json("I cannot help with that.")


def test_a_schema_violation_is_an_error_not_a_score():
    """JSON mode guarantees JSON, not the right JSON."""
    bad = json.dumps({"fit_score": "very good", "verdict": "possible"})
    scorer = scorer_for(lambda r: httpx.Response(200, json=completion(bad)))
    result = scorer.score_one(make_job())
    assert not result.ok and "could not parse" in result.error


def test_an_out_of_range_score_is_clamped_not_thrown_away():
    """Weaker models overshoot the 0-100 range. FitScore clamps deliberately —
    a 900 means "very high", and binning the whole call over it would cost a
    request to learn nothing."""
    bad = json.dumps({**VALID_FIT, "fit_score": 900})
    scorer = scorer_for(lambda r: httpx.Response(200, json=completion(bad)))
    result = scorer.score_one(make_job())
    assert result.ok and result.score.fit_score == 100


def test_an_empty_response_is_an_error():
    scorer = scorer_for(lambda r: httpx.Response(200, json=completion("   ")))
    assert "empty response" in scorer.score_one(make_job()).error


def test_running_out_of_context_names_the_numbers_and_the_fix():
    """The real cause is the context window, not the output cap — Ollama
    ships 4096 and the rubric plus resume is ~2.4k before the JD. "Truncated"
    alone sends people hunting for the wrong setting."""
    body = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 2430, "completion_tokens": 1666, "total_tokens": 4096},
    }
    scorer = scorer_for(lambda r: httpx.Response(200, json=body))
    error = scorer.score_one(make_job()).error
    assert "4096" in error and "2430" in error
    assert "OLLAMA_CONTEXT_LENGTH" in error


def test_reasoning_is_switched_off_where_the_endpoint_understands():
    """The schema constrains the answer already, and on a local model the
    thinking is most of the wall clock."""
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer_for(handler).score_one(make_job())
    assert seen["think"] is False
    assert seen["max_tokens"] == 8000


def test_reasoning_models_get_the_other_token_field():
    """OpenAI's reasoning models reject max_tokens by name."""
    fields = []

    def handler(request):
        body = json.loads(request.content)
        fields.append("max_completion_tokens" if "max_completion_tokens" in body else "max_tokens")
        if "max_completion_tokens" not in body:
            return httpx.Response(400, text="Unsupported parameter: use 'max_completion_tokens'")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    result = scorer_for(handler).score_one(make_job())
    assert result.ok
    assert fields == ["max_tokens", "max_completion_tokens"]


def test_cached_prompt_tokens_are_recorded_when_reported():
    body = completion(json.dumps(VALID_FIT), prompt_tokens_details={"cached_tokens": 88})
    scorer = scorer_for(lambda r: httpx.Response(200, json=body))
    assert scorer.score_one(make_job()).cache_read_tokens == 88


# ----------------------------------------------------------------------
# Failure modes that matter for local servers


def test_a_refused_connection_names_the_endpoint():
    """The overwhelmingly likely cause is "Ollama isn't running" — say that
    rather than surfacing a bare ConnectError."""

    def handler(request):
        raise httpx.ConnectError("connection refused")

    scorer = scorer_for(handler, base_url="http://localhost:11434/v1", api_key=None)
    result = scorer.score_one(make_job())
    assert not result.ok
    assert "localhost:11434" in result.error and "is the server running" in result.error


def test_a_server_error_is_reported_not_raised():
    scorer = scorer_for(lambda r: httpx.Response(500, text="internal error"))
    result = scorer.score_one(make_job())
    assert not result.ok and "API 500" in result.error


def test_score_many_keeps_going_after_a_failure():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    results = scorer_for(handler).score_many([make_job(1), make_job(2)])
    assert [r.ok for r in results] == [False, True]


def test_an_empty_resume_is_refused_up_front():
    from jobbot.scoring.matcher import ScoringError

    with pytest.raises(ScoringError):
        OpenAICompatScorer("   ", model="gpt-5", base_url="https://x/v1")


def test_a_missing_endpoint_is_refused_with_a_useful_message():
    from jobbot.scoring.matcher import ScoringError

    with pytest.raises(ScoringError, match="base_url"):
        OpenAICompatScorer(RESUME, model="m", base_url="")


# ----------------------------------------------------------------------
# Provider selection


def test_anthropic_keeps_its_own_scorer():
    """It is the only path with prompt caching and a batch endpoint."""
    from jobbot.scoring.matcher import Matcher

    scorer = build_scorer(RESUME, ModelConfig(provider="anthropic"))
    assert isinstance(scorer, Matcher)
    assert scorer.supports_batch is True


@pytest.mark.parametrize(
    "provider, host",
    [
        ("openai", "api.openai.com"),
        ("gemini", "generativelanguage.googleapis.com"),
        ("ollama", "localhost:11434"),
        ("lmstudio", "localhost:1234"),
    ],
)
def test_every_other_provider_uses_the_compatible_client(provider, host):
    scorer = build_scorer(RESUME, ModelConfig(provider=provider, scoring="m"))
    assert isinstance(scorer, OpenAICompatScorer)
    assert host in scorer.base_url
    assert scorer.supports_batch is False


def test_base_url_overrides_the_preset():
    cfg = ModelConfig(provider="ollama", base_url="http://192.168.1.50:8080/v1", scoring="m")
    assert build_scorer(RESUME, cfg).base_url == "http://192.168.1.50:8080/v1"


def test_a_trailing_slash_does_not_double_up():
    cfg = ModelConfig(provider="custom", base_url="http://x/v1/", scoring="m")
    assert build_scorer(RESUME, cfg).base_url == "http://x/v1"


# ----------------------------------------------------------------------
# Config helpers


@pytest.mark.parametrize(
    "provider, env, needs",
    [
        ("anthropic", "ANTHROPIC_API_KEY", True),
        ("openai", "OPENAI_API_KEY", True),
        ("gemini", "GEMINI_API_KEY", True),
        ("ollama", None, False),
        ("lmstudio", None, False),
    ],
)
def test_each_provider_knows_its_key_variable(provider, env, needs):
    cfg = ModelConfig(provider=provider)
    assert cfg.key_env() == env
    assert cfg.needs_key() is needs


def test_api_key_env_can_be_overridden():
    cfg = ModelConfig(provider="custom", api_key_env="MY_GATEWAY_TOKEN")
    assert cfg.key_env() == "MY_GATEWAY_TOKEN"
    assert cfg.needs_key() is True


def test_a_local_model_is_not_billed_at_cloud_rates():
    """Reporting a dollar figure for a llama on your own laptop is just wrong."""
    from jobbot.scoring.matcher import ScoringStats

    stats = ScoringStats(input_tokens=1_000_000, output_tokens=1_000_000)
    assert stats.estimated_cost_usd("llama3", "ollama") == 0.0
    assert stats.estimated_cost_usd("claude-opus-5", "anthropic") > 0


def test_a_rejected_temperature_is_dropped_rather_than_failing_the_run():
    """Found against a live gpt-5.x model: it allows only the default
    temperature and 400s on 0. Determinism is worth asking for and not worth
    dying over."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("temperature" in body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unsupported value: 'temperature' does not support 0 with this model.",
                "type": "invalid_request_error", "param": "temperature"}})
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer = scorer_for(handler)
    assert scorer.score_one(make_job()).ok
    assert seen == [True, False]


def test_an_unknown_parameter_is_dropped_and_stays_dropped():
    """OpenAI 400s on an unrecognised parameter rather than ignoring it, which
    is how `think` — added for Ollama — broke OpenAI outright."""
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(sorted(k for k in body if k in ("think", "reasoning_effort", "temperature")))
        if "think" in body:
            return httpx.Response(400, json={"error": {
                "message": "Unknown parameter: 'think'.",
                "type": "invalid_request_error",
                "param": "think", "code": "unknown_parameter"}})
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer = scorer_for(handler)
    assert scorer.score_one(make_job(1)).ok
    assert scorer.score_one(make_job(2)).ok
    assert "think" not in scorer._extras
    # The second job must not relearn it.
    assert all("think" not in call for call in sent[1:])


def test_dropping_extras_terminates_even_if_everything_is_rejected():
    """A server that refuses every optional parameter must still get an
    answer or a clean error — never an unbounded retry loop."""
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(1)
        for name in ("temperature", "think", "reasoning_effort"):
            if name in body:
                return httpx.Response(400, json={"error": {
                    "message": f"Unknown parameter: '{name}'.", "param": name}})
        return httpx.Response(200, json=completion(json.dumps(VALID_FIT)))

    scorer = scorer_for(handler)
    assert scorer.score_one(make_job()).ok
    assert len(calls) <= len(_OPTIONAL_EXTRAS) + 2
    assert scorer._extras == set()

"""Scoring through providers other than Claude.

Almost everything now speaks OpenAI's `/v1/chat/completions` shape — OpenAI
itself, Gemini through its compatibility endpoint, Ollama, LM Studio, vLLM,
llama.cpp's server, and most hosted gateways. So there is one client here
rather than one per vendor, and a provider is a base URL plus a key.

Deliberately built on `httpx` (already a dependency) rather than the `openai`
SDK. The request is a single non-streaming JSON POST; a second SDK would buy
nothing and would not cover the local servers this is mostly for.

**The native Claude path is not routed through here.** It keeps prompt caching
and the Batches API, which this path has no equivalent for — see the cost note
in `build_scorer`.

Structured output is the fiddly part. Support is uneven:

- OpenAI and recent Gemini honour `response_format: json_schema` strictly.
- Ollama and LM Studio accept `json_object`, and newer builds accept a schema.
- Some gateways reject an unknown `response_format` outright with a 400.

So the request walks down a ladder — schema, then json_object, then plain
prompting — remembering the first rung that worked. Whatever comes back is
validated against `FitScore` regardless, because a model claiming JSON mode
is not the same as a model emitting valid JSON.
"""

from __future__ import annotations

import json
import re

import httpx
from pydantic import ValidationError

from ..config import ModelConfig
from ..db import Job
from .matcher import MAX_TOKENS, ScoreResult, ScoringError
from .prompts import system_blocks, user_message
from .schema import FitScore

#: Response-format modes, most to least capable. A 400 mentioning the field
#: drops to the next one.
_MODES = ("json_schema", "json_object", "prompt")

#: Parameters worth sending but not worth failing over. Support is
#: inconsistent and disagreement is not graceful — OpenAI 400s on an
#: unrecognised name rather than ignoring it (which is how `think` broke
#: OpenAI outright), and its reasoning models reject `temperature: 0` because
#: they only allow the default. Each is dropped permanently the first time an
#: endpoint objects, so a run degrades instead of dying.
#:
#: `temperature` is here reluctantly: 0 is what keeps a posting from swinging
#: ten points between runs. Models that refuse it leave no choice.
_OPTIONAL_EXTRAS = {"temperature": 0, "think": False, "reasoning_effort": "low"}

DEFAULT_READ_TIMEOUT = 600.0
"""Ten minutes. Measured, not guessed: a 9B reasoning model on a laptop took
over five minutes for one posting at a 16k context. Connect stays short — a
wrong port should fail immediately rather than hang."""


def _timeout(read_seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=read_seconds, write=30.0, pool=10.0)


def _system_text(resume_text: str) -> str:
    """The cacheable Anthropic blocks, flattened to one system string.

    None of these providers have Anthropic's explicit cache breakpoints, so
    the rubric and resume are simply concatenated. OpenAI and Gemini both do
    automatic prefix caching, which this ordering still benefits from — the
    invariant bytes stay in front.
    """
    return "\n\n".join(block["text"] for block in system_blocks(resume_text, cache=False))


def _extract_json(text: str) -> dict:
    """Pull the FitScore object out of a response.

    Models that cannot be held to a schema still tend to produce the right
    object wrapped in prose or a ```json fence. Take the outermost braces
    rather than giving up.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start : end + 1])


class OpenAICompatScorer:
    """Scores one job per request against any OpenAI-compatible endpoint."""

    supports_batch = False

    def __init__(
        self,
        resume_text: str,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        if not resume_text.strip():
            raise ScoringError("Resume text is empty — nothing to score against.")
        if not base_url:
            raise ScoringError(
                "No endpoint configured. Set model.base_url in config/search.yaml "
                "(for provider: custom), or pick a provider with a known one."
            )
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._system = _system_text(resume_text)
        self._mode = _MODES[0]
        # OpenAI's reasoning models reject `max_tokens` and demand
        # `max_completion_tokens`; everything else still wants the former.
        # Start compatible and swap once if the endpoint objects.
        self._token_field = "max_tokens"
        self._extras = set(_OPTIONAL_EXTRAS)
        self._client = client or httpx.Client(timeout=_timeout(read_timeout))

    # ------------------------------------------------------------------

    def _payload(self, job: Job, mode: str) -> dict:
        instruction = ""
        if mode == "prompt":
            # Last resort: the schema goes in the prompt because the endpoint
            # will not enforce one.
            instruction = (
                "\n\nReply with ONLY a JSON object matching this schema, "
                "no prose and no code fence:\n"
                + json.dumps(FitScore.model_json_schema())
            )

        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system + instruction},
                {
                    "role": "user",
                    "content": user_message(
                        title=job.title,
                        company=job.company,
                        location=job.location,
                        description=job.description,
                    ),
                },
            ],
        }

        # Without an explicit ceiling the server picks its own, and local ones
        # pick small: Ollama's default cut a real FitScore off mid-object and
        # reported it as truncated. Reasoning models spend part of this budget
        # thinking, so it needs headroom rather than just enough for the JSON.
        payload[self._token_field] = MAX_TOKENS

        # Determinism, and reasoning off where the endpoint understands how:
        # the schema already constrains the answer, and on a local model the
        # thinking is most of the wall clock — a 9B spent its whole output
        # budget on it and returned an empty string. Anything the endpoint
        # objects to is dropped and remembered — see `_OPTIONAL_EXTRAS`.
        for name, value in _OPTIONAL_EXTRAS.items():
            if name in self._extras:
                payload[name] = value
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "fit_score",
                    "strict": True,
                    "schema": FitScore.model_json_schema(),
                },
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return self._client.post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        )

    def _rejected_extra(self, response: httpx.Response) -> str | None:
        """The name of an optional parameter the endpoint refused, if any.

        OpenAI reports it as `error.param`; others only put it in the message.
        Either way the fix is the same — drop it and try again.
        """
        if response.status_code != 400:
            return None
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        named = error.get("param")
        if named in self._extras:
            return named
        for name in list(self._extras):
            if f"'{name}'" in response.text or f'"{name}"' in response.text:
                return name
        return None

    def score_one(self, job: Job) -> ScoreResult:
        # Walk down the ladder, but only past rungs the endpoint rejects.
        for mode in _MODES[_MODES.index(self._mode) :]:
            # Bounded: each pass either succeeds or permanently drops one
            # optional parameter, and there are only a handful of those.
            for _ in range(len(_OPTIONAL_EXTRAS) + 2):
                try:
                    response = self._post(self._payload(job, mode))
                except httpx.ConnectError as exc:
                    return ScoreResult(
                        job_id=job.id,
                        error=f"could not reach {self.base_url} — is the server running? ({exc})",
                    )
                except httpx.HTTPError as exc:
                    return ScoreResult(job_id=job.id, error=f"request failed: {exc}")

                if response.status_code == 400 and "max_completion_tokens" in response.text:
                    # Reasoning models on OpenAI: same request, different spelling.
                    self._token_field = "max_completion_tokens"
                    continue

                if rejected := self._rejected_extra(response):
                    self._extras.discard(rejected)  # remembered for every later job
                    continue

                break

            if response.status_code == 400 and "response_format" in response.text:
                self._mode = mode  # remember the failure so the next job skips it
                continue
            if response.status_code >= 400:
                return ScoreResult(
                    job_id=job.id,
                    error=f"API {response.status_code}: {response.text[:200]}",
                )

            self._mode = mode
            return self._parse(job, response.json())

        return ScoreResult(job_id=job.id, error="endpoint rejected every response format")

    def _parse(self, job: Job, body: dict) -> ScoreResult:
        choices = body.get("choices") or []
        if not choices:
            return ScoreResult(job_id=job.id, error="no choices in response")

        usage = body.get("usage") or {}
        result = ScoreResult(
            job_id=job.id,
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            # Reported by providers that do automatic prefix caching.
            cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
        )

        choice = choices[0]
        if choice.get("finish_reason") == "length":
            # Almost always the *context window*, not the output cap: local
            # servers default small (Ollama ships 4096), and the rubric plus
            # resume alone is ~2.4k before the job description is added.
            total = usage.get("total_tokens") or (result.input_tokens + result.output_tokens)
            result.error = (
                f"ran out of room at {total} tokens "
                f"(prompt {result.input_tokens} + output {result.output_tokens}). "
                "Raise the model's context window — for Ollama, restart it with "
                "OLLAMA_CONTEXT_LENGTH=16384"
            )
            return result

        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            result.error = "empty response"
            return result

        try:
            result.score = FitScore.model_validate(_extract_json(text))
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            result.error = f"could not parse response: {str(exc)[:200]}"
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


def build_scorer(resume_text: str, model_cfg: ModelConfig, *, api_key: str | None = None):
    """Pick the scorer for the configured provider.

    Claude keeps its own path. That is not favouritism — it is the only one
    with explicit prompt caching (the resume is re-sent on *every* job here,
    so an OpenAI-compatible run costs meaningfully more per job) and the only
    one with a half-price batch endpoint.
    """
    if model_cfg.is_native_anthropic:
        from .matcher import Matcher

        return Matcher(resume_text, model=model_cfg.scoring, effort=model_cfg.effort)

    return OpenAICompatScorer(
        resume_text,
        model=model_cfg.scoring,
        base_url=model_cfg.endpoint(),
        api_key=api_key,
    )

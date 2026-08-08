"""Shared test fixtures and fakes."""

from __future__ import annotations

from types import SimpleNamespace

from jobbot.db import Job

RESUME = "Jane Doe. Backend engineer. Python, Go, PostgreSQL, Kafka. 6 years." * 5

VALID = {
    "fit_score": 82,
    "verdict": "possible",
    "strengths": ["6 years of Python", "Kafka in production"],
    "gaps": ["no Kubernetes experience shown"],
    "blockers": [],
    "tailored_summary": "Six years building Python services at scale.",
}


def make_job(job_id: int = 1, **kwargs) -> Job:
    base = {
        "id": job_id,
        "source": "instahyre",
        "source_job_id": str(job_id),
        "url": "https://x/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Bangalore",
        "description": "We want a backend engineer with Python and Kafka.",
    }
    return Job(**{**base, **kwargs})


def usage(**kwargs):
    base = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    return SimpleNamespace(**{**base, **kwargs})

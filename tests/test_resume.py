import pytest

from jobbot.resume import ResumeError, _clean, extract_text, get_resume_text

SAMPLE = """Jane Doe
Backend Engineer

EXPERIENCE
Acme Corp — Senior Backend Engineer (2021-present)
  Built payment services in Python and Go, handling 2M requests/day.
  Led migration from monolith to services.

Globex — Backend Engineer (2018-2021)
  Django REST APIs, PostgreSQL, Celery.

SKILLS
Python, Go, PostgreSQL, Kafka, Docker, Kubernetes, AWS

EDUCATION
B.Tech Computer Science, 2018
"""


class TestClean:
    """Cleaning must be deterministic — the output becomes a cached prompt
    prefix, and unstable bytes silently destroy the cache hit rate."""

    def test_collapses_runs_of_spaces(self):
        assert _clean("a      b") == "a b"

    def test_normalizes_line_endings(self):
        assert _clean("a\r\nb") == "a\nb"
        assert _clean("a\rb") == "a\nb"

    def test_collapses_excess_blank_lines(self):
        assert _clean("a\n\n\n\n\nb") == "a\n\nb"

    def test_strips_trailing_whitespace_per_line(self):
        assert _clean("a   \nb   ") == "a\nb"

    def test_is_idempotent(self):
        once = _clean(SAMPLE)
        assert _clean(once) == once

    def test_removes_control_characters(self):
        assert "\x00" not in _clean("a\x00b")


class TestExtract:
    def test_reads_plain_text(self, tmp_path):
        path = tmp_path / "resume.txt"
        path.write_text(SAMPLE)
        text = extract_text(path)
        assert "Acme Corp" in text
        assert "Kubernetes" in text

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ResumeError, match="not found"):
            extract_text(tmp_path / "nope.pdf")

    def test_unsupported_format_raises(self, tmp_path):
        path = tmp_path / "resume.pages"
        path.write_text(SAMPLE)
        with pytest.raises(ResumeError, match="Unsupported"):
            extract_text(path)

    def test_near_empty_extraction_raises_with_scan_hint(self, tmp_path):
        # A scanned-image PDF extracts almost nothing. Fail loudly rather than
        # scoring every job against a blank resume.
        path = tmp_path / "resume.txt"
        path.write_text("Jane Doe")
        with pytest.raises(ResumeError, match="scanned image"):
            extract_text(path)


class TestCache:
    def test_reuses_cache_until_source_changes(self, tmp_path, monkeypatch):
        import jobbot.resume as resume_mod

        monkeypatch.setattr(resume_mod, "CACHE_PATH", tmp_path / "resume.txt.cache")
        monkeypatch.setattr(resume_mod, "HASH_PATH", tmp_path / "resume.sha256")

        source = tmp_path / "resume.txt"
        source.write_text(SAMPLE)

        first = get_resume_text(source)
        assert "Acme Corp" in first
        assert resume_mod.CACHE_PATH.exists()

        # Editing the resume must invalidate — otherwise scoring silently runs
        # against a stale document.
        source.write_text(SAMPLE.replace("Acme Corp", "Initech"))
        second = get_resume_text(source)
        assert "Initech" in second
        assert "Acme Corp" not in second

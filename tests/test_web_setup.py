"""Control-panel tests: setup, account connection, and running commands.

The security-relevant assertion here is the absence of something — there is
no route, form field, or storage path anywhere that accepts a job-portal
password. Sites are connected by launching a browser you log into yourself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobbot import config as config_mod
from jobbot import db as db_mod
from jobbot import resume as resume_mod
from jobbot.browser import session as browser_session_mod
from jobbot.web import setup as setup_mod


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Point every path at a scratch directory."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    # ROOT too: relative resume paths in profile.yaml resolve against it.
    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(setup_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(setup_mod, "PROFILE_PATH", config_dir / "profile.yaml")
    monkeypatch.setattr(setup_mod, "RESUME_DIR", data_dir)
    # browser.session binds DATA_DIR at import time, so patching config alone
    # would leave has_profile() reading the developer's real sessions.
    monkeypatch.setattr(browser_session_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(resume_mod, "CACHE_PATH", data_dir / ".resume-cache.txt")
    monkeypatch.setattr(resume_mod, "HASH_PATH", data_dir / ".resume-cache.sha256")
    monkeypatch.setattr(db_mod, "DB_PATH", data_dir / "jobs.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)

    config_mod.load_profile.cache_clear()
    db_mod.init_db(f"sqlite:///{data_dir / 'jobs.db'}")
    yield tmp_path
    config_mod.load_profile.cache_clear()


@pytest.fixture()
def client(app_env):
    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        yield client


PROFILE_FORM = {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@example.com",
    "phone": "+91 99999 99999",
    "city": "Bangalore",
    "country": "India",
    "current_company": "Acme",
    "total_years_experience": "6",
    "notice_period_days": "60",
    "expected_ctc": "26 LPA",
    "authorized_to_work_in": "India, Singapore",
    "willing_to_relocate": "on",
    "preferred_work_mode": "hybrid",
}

RESUME_BYTES = b"Jane Doe. Backend engineer. Six years of Python, Go, Kafka, PostgreSQL. " * 6


class TestPagesRender:
    def test_setup_page(self, client):
        assert client.get("/setup").status_code == 200

    def test_run_page(self, client):
        assert client.get("/run").status_code == 200

    def test_setup_lists_every_known_site(self, client):
        from jobbot.scrapers.registry import SCRAPERS

        text = client.get("/setup").text
        for site in SCRAPERS:
            assert site in text


class TestNoCredentialHandling:
    """The tool must never be able to accept or store a portal password."""

    def test_setup_page_has_no_password_field(self, client):
        text = client.get("/setup").text.lower()
        assert 'type="password"' not in text
        assert "type='password'" not in text

    def test_connect_form_only_carries_a_site_name(self, client):
        import inspect

        from jobbot.web.app import connect_site

        params = set(inspect.signature(connect_site).parameters)
        assert params == {"site"}

    def test_profile_model_has_no_credential_fields(self):
        from jobbot.config import Profile

        rendered = str(Profile.model_json_schema()).lower()
        for term in ("password", "passwd", "secret"):
            assert term not in rendered


class TestProfileEditing:
    def test_saving_writes_a_valid_profile(self, client):
        response = client.post("/setup/profile", data=PROFILE_FORM)
        assert response.status_code == 303

        profile = setup_mod.current_profile()
        assert profile.identity.full_name == "Jane Doe"
        assert profile.employment.expected_ctc == "26 LPA"
        assert profile.eligibility.authorized_to_work_in == ["India", "Singapore"]
        assert profile.eligibility.willing_to_relocate is True

    def test_unchecked_boxes_are_saved_as_false(self, client):
        client.post("/setup/profile", data=PROFILE_FORM)  # no sponsorship key
        assert setup_mod.current_profile().eligibility.requires_visa_sponsorship is False

    def test_saved_values_come_back_into_the_form(self, client):
        client.post("/setup/profile", data=PROFILE_FORM)
        assert "jane@example.com" in client.get("/setup").text

    @pytest.mark.parametrize("mode", ["remote", "hybrid", "onsite", ""])
    def test_work_mode_round_trips(self, client, mode):
        """The segmented control replaced a <select>; it must post the same
        way, empty value included."""
        client.post("/setup/profile", data={**PROFILE_FORM, "preferred_work_mode": mode})
        assert setup_mod.current_profile().eligibility.preferred_work_mode == mode

    def test_work_mode_renders_as_radios_not_a_popup(self, client):
        # A four-option popup that covers the page is the wrong control; all
        # four choices should be visible at once.
        text = client.get("/setup").text
        assert 'class="segmented"' in text
        assert text.count('name="preferred_work_mode"') == 4
        assert 'role="radiogroup"' in text

    def test_saved_work_mode_comes_back_checked(self, client):
        client.post("/setup/profile", data={**PROFILE_FORM, "preferred_work_mode": "onsite"})
        text = client.get("/setup").text
        onsite = text.split('value="onsite"')[1][:40]
        assert "checked" in onsite

    def test_non_numeric_years_does_not_crash(self, client):
        response = client.post("/setup/profile", data={**PROFILE_FORM, "total_years_experience": "six"})
        assert response.status_code == 303
        assert setup_mod.current_profile().employment.total_years_experience == 0


class TestResumeUpload:
    def test_upload_stores_and_parses(self, client, app_env):
        response = client.post(
            "/setup/resume", files={"resume": ("cv.txt", RESUME_BYTES, "text/plain")}
        )
        assert response.status_code == 303
        assert "saved=resume" in response.headers["location"]

        ready = setup_mod.readiness()
        assert ready.resume_ok
        assert ready.resume_name == "resume.txt"

    def test_uploaded_filename_never_becomes_the_path(self, client, app_env):
        # A browser-supplied filename is attacker-controlled input.
        client.post(
            "/setup/resume",
            files={"resume": ("../../etc/passwd.txt", RESUME_BYTES, "text/plain")},
        )
        assert (app_env / "data" / "resume.txt").exists()
        assert not (app_env / "etc").exists()

    def test_source_file_is_not_clobbered_by_the_extraction_cache(self, client, app_env):
        # A .txt resume and the cache must not share a path.
        client.post("/setup/resume", files={"resume": ("cv.txt", RESUME_BYTES, "text/plain")})
        assert (app_env / "data" / "resume.txt").read_bytes() == RESUME_BYTES

    def test_changing_format_removes_the_previous_file(self, client, app_env):
        client.post("/setup/resume", files={"resume": ("cv.txt", RESUME_BYTES, "text/plain")})
        client.post("/setup/resume", files={"resume": ("cv.md", RESUME_BYTES, "text/markdown")})
        assert (app_env / "data" / "resume.md").exists()
        assert not (app_env / "data" / "resume.txt").exists()

    @pytest.mark.parametrize("name", ["virus.exe", "photo.png", "archive.zip", "noextension"])
    def test_unsupported_types_are_rejected(self, client, name):
        response = client.post("/setup/resume", files={"resume": (name, b"data", "application/x")})
        assert "error=" in response.headers["location"]

    def test_empty_file_is_rejected(self, client):
        response = client.post("/setup/resume", files={"resume": ("cv.txt", b"", "text/plain")})
        assert "error=" in response.headers["location"]

    def test_unparseable_resume_surfaces_at_upload_time(self, client):
        # A scanned-image PDF extracts nothing. Better to fail here than to
        # score every job against a blank document.
        response = client.post("/setup/resume", files={"resume": ("cv.txt", b"Jane", "text/plain")})
        assert "error=" in response.headers["location"]


class TestReadiness:
    def test_starts_unready(self, app_env):
        ready = setup_mod.readiness()
        assert not ready.profile_ok
        assert not ready.resume_ok
        assert not ready.can_apply

    def test_apply_needs_both_profile_and_resume(self, client):
        client.post("/setup/profile", data=PROFILE_FORM)
        assert not setup_mod.readiness().can_apply  # no resume yet

        client.post("/setup/resume", files={"resume": ("cv.txt", RESUME_BYTES, "text/plain")})
        assert setup_mod.readiness().can_apply

    def test_discover_needs_a_connected_site(self, app_env):
        assert not setup_mod.readiness().can_discover


class TestRunGuards:
    def test_unknown_command_is_refused(self, client):
        response = client.post("/run", data={"command": "rm-rf"})
        assert "error=" in response.headers["location"]

    def test_real_submit_requires_the_confirmation_box(self, client):
        # The submit checkbox alone is too easy to leave ticked from a
        # previous run; sending real applications takes a second, explicit act.
        response = client.post("/run", data={"command": "apply", "submit": "on"})
        assert "error=" in response.headers["location"]
        assert "confirmation" in response.headers["location"].lower()

    def test_unknown_site_is_refused(self, client):
        response = client.post("/setup/connect", data={"site": "monster"})
        assert "error=" in response.headers["location"]

    def test_stream_endpoint_is_safe_when_idle(self, client):
        response = client.get("/run/stream")
        assert response.status_code == 200


class TestCommandBuilding:
    """The arguments handed to the CLI, without actually spawning anything."""

    @pytest.fixture(autouse=True)
    def _capture(self, monkeypatch):
        from jobbot.web import app as app_mod

        self.started: list[list[str]] = []

        class FakeTasks:
            current = None

            def start(_self, args, label):
                self.started.append(args)
                return type("T", (), {"id": "x", "label": label})()

            def history(_self):
                return []

            def get(_self, _id):
                return None

        monkeypatch.setattr(app_mod, "TASKS", FakeTasks())

    def test_discover_passes_site_and_limit(self, client):
        client.post("/run", data={"command": "discover", "site": "instahyre", "limit": "5"})
        assert self.started == [["discover", "--site", "instahyre", "--limit", "5"]]

    def test_blank_limit_is_omitted(self, client):
        client.post("/run", data={"command": "discover", "site": "", "limit": ""})
        assert self.started == [["discover"]]

    def test_non_numeric_limit_is_ignored(self, client):
        client.post("/run", data={"command": "score", "limit": "; rm -rf /"})
        assert self.started == [["score"]]

    def test_apply_defaults_to_a_dry_run(self, client):
        client.post("/run", data={"command": "apply"})
        assert self.started == [["apply"]]
        assert "--submit" not in self.started[0]

    def test_confirmed_submit_passes_the_flag(self, client):
        client.post("/run", data={"command": "apply", "submit": "on", "confirm": "yes"})
        assert self.started == [["apply", "--submit", "--yes"]]

    def test_connect_launches_the_manual_login_flow(self, client):
        client.post("/setup/connect", data={"site": "instahyre"})
        assert self.started == [["login", "instahyre"]]

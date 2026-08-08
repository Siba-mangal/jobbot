"""The Search screen — the shared sweep, and what saving it costs.

Two things here are worth pinning beyond "the form round-trips".

The first is that `load_search_config` is `lru_cache`d for the process
lifetime. A save that forgets to invalidate it leaves the running server —
and every later reader — serving the values from before the edit, which looks
exactly like the save silently failing.

The second is that this screen models ONE sweep while the config models a
query *list per site*. Saving therefore replaces those lists. The LinkedIn
1h/24h pair lives in one, so the screen has to say what it is about to drop
before it drops it.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from jobbot import config as config_mod
from jobbot import db as db_mod
from jobbot.browser import session as browser_session_mod
from jobbot.web import setup as setup_mod

BASE_YAML = {
    "sites": {
        "instahyre": {"enabled": True, "daily_cap": 100,
                      "queries": [{"keywords": "backend engineer", "location": "Bangalore"}]},
        "cutshort": {"enabled": False, "daily_cap": 100, "queries": []},
        "linkedin": {"enabled": True, "daily_cap": 40, "queries": [
            {"keywords": "backend engineer", "location": "India",
             "posted_within_hours": 1, "label": "Fresh — last hour"},
            {"keywords": "backend engineer", "location": "India",
             "posted_within_hours": 24, "label": "Today — last 24 hours"},
        ]},
    },
    "prefilter": {"exclude_title_keywords": ["intern", "sales"]},
    "review": {"min_score": 60},
    "apply": {"max_per_day": 20},
}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Scratch config + data. Nothing here may touch the real search.yaml."""
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    monkeypatch.setattr(config_mod, "ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(setup_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(setup_mod, "PROFILE_PATH", config_dir / "profile.yaml")
    monkeypatch.setattr(setup_mod, "RESUME_DIR", data_dir)
    monkeypatch.setattr(browser_session_mod, "DATA_DIR", data_dir)
    monkeypatch.setattr(db_mod, "DB_PATH", data_dir / "jobs.db")
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)

    (config_dir / "search.yaml").write_text(yaml.safe_dump(BASE_YAML))
    config_mod.load_search_config.cache_clear()
    config_mod.load_profile.cache_clear()
    db_mod.init_db(f"sqlite:///{data_dir / 'jobs.db'}")
    yield config_dir
    config_mod.load_search_config.cache_clear()
    config_mod.load_profile.cache_clear()


@pytest.fixture()
def client(env):
    from jobbot.web.app import app

    with TestClient(app, follow_redirects=False) as client:
        yield client


def written(env) -> dict:
    return yaml.safe_load((env / "search.yaml").read_text())


FORM = {
    "keywords": "platform engineer",
    "location": "Remote",
    "fresh": "24h",
    "min_score": "72",
    "excludes": "intern, sales, recruiter",
    "boards": ["instahyre", "cutshort"],
    "then": "save",
}


# ----------------------------------------------------------------------
# Round trip


def test_the_form_is_seeded_from_the_config(client):
    text = client.get("/search").text
    assert 'value="backend engineer"' in text
    assert 'value="Bangalore"' in text


def test_saving_writes_the_shared_query_to_every_site(client, env):
    assert client.post("/search", data=FORM).status_code == 303
    sites = written(env)["sites"]
    for site in ("instahyre", "cutshort", "linkedin"):
        assert len(sites[site]["queries"]) == 1
        assert sites[site]["queries"][0]["keywords"] == "platform engineer"
        assert sites[site]["queries"][0]["location"] == "Remote"


def test_board_toggles_are_written(client, env):
    client.post("/search", data=FORM)
    sites = written(env)["sites"]
    assert sites["instahyre"]["enabled"] is True
    assert sites["cutshort"]["enabled"] is True
    assert sites["linkedin"]["enabled"] is False  # not in the posted board list


def test_unchecking_every_board_disables_them_all(client, env):
    client.post("/search", data={**FORM, "boards": []})
    assert not any(s["enabled"] for s in written(env)["sites"].values())


def test_min_score_and_excludes_are_written(client, env):
    client.post("/search", data=FORM)
    cfg = written(env)
    assert cfg["review"]["min_score"] == 72
    assert cfg["prefilter"]["exclude_title_keywords"] == ["intern", "sales", "recruiter"]


def test_blank_excludes_clears_the_list(client, env):
    client.post("/search", data={**FORM, "excludes": "  ,  "})
    assert written(env)["prefilter"]["exclude_title_keywords"] == []


@pytest.mark.parametrize("choice, hours", [("1h", 1), ("24h", 24), ("7d", 168)])
def test_freshness_maps_to_hours(client, env, choice, hours):
    client.post("/search", data={**FORM, "fresh": choice})
    query = written(env)["sites"]["instahyre"]["queries"][0]
    assert query["posted_within_hours"] == hours


def test_any_freshness_writes_no_window(client, env):
    client.post("/search", data={**FORM, "fresh": "any"})
    query = written(env)["sites"]["instahyre"]["queries"][0]
    assert "posted_within_hours" not in query
    assert "posted_within_days" not in query


def test_remote_only_round_trips(client, env):
    client.post("/search", data={**FORM, "remote": "on"})
    assert written(env)["sites"]["instahyre"]["queries"][0]["remote"] is True


def test_min_score_is_clamped(client, env):
    client.post("/search", data={**FORM, "min_score": "999"})
    assert written(env)["review"]["min_score"] == 100


# ----------------------------------------------------------------------
# The cache


def test_saving_invalidates_the_cached_config(client):
    """The failure this guards is silent: the file changes, readers don't."""
    assert config_mod.load_search_config().review.min_score == 60
    client.post("/search", data=FORM)
    assert config_mod.load_search_config().review.min_score == 72


def test_the_page_shows_the_new_values_immediately_after_saving(client):
    client.post("/search", data=FORM)
    text = client.get("/search").text
    assert 'value="platform engineer"' in text
    assert 'value="72"' in text


# ----------------------------------------------------------------------
# Nothing is dropped quietly


def test_extra_queries_are_named_before_they_are_replaced(client):
    """Saving discards them, so the page has to list them first."""
    text = client.get("/search").text
    assert "Today — last 24 hours" in text
    assert "replaces" in text.lower()


def test_no_warning_when_there_is_nothing_to_lose(client, env):
    client.post("/search", data=FORM)          # collapses every site to one query
    assert "Saving replaces" not in client.get("/search").text


# ----------------------------------------------------------------------
# The regenerated guidance


def test_the_linkedin_warning_survives_a_save(client, env):
    """PyYAML cannot round-trip comments, so the warning is re-emitted.

    Regenerating beats preserving here: it cannot be lost by an edit either.
    """
    client.post("/search", data=FORM)
    text = (env / "search.yaml").read_text()
    assert "User Agreement prohibits automated access" in text
    # and it sits with the site it is about, not orphaned at the top
    assert text.index("prohibits automated access") < text.index("  linkedin:")


def test_the_saved_file_still_parses(client, env):
    client.post("/search", data=FORM)
    config_mod.load_search_config.cache_clear()
    cfg = config_mod.load_search_config()
    assert cfg.review.min_score == 72
    assert cfg.sites["instahyre"].queries[0].keywords == "platform engineer"


# ----------------------------------------------------------------------
# Handing off to a run


def test_run_discover_starts_a_task_and_redirects(client, monkeypatch):
    started = {}
    from jobbot.web import app as app_mod

    monkeypatch.setattr(
        app_mod.TASKS, "start", lambda args, label: started.update(args=args, label=label)
    )
    response = client.post("/search", data={**FORM, "then": "discover"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/run")
    assert started["args"] == ["discover"]


def test_save_only_stays_on_the_search_page(client):
    response = client.post("/search", data=FORM)
    assert response.headers["location"].startswith("/search")

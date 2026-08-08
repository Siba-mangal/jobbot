import pytest

from jobbot import db as db_mod


@pytest.fixture()
def session(tmp_path, monkeypatch):
    """A throwaway SQLite database per test."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionFactory", None)
    db_mod.init_db(url)
    with db_mod.session_scope(url) as session:
        yield session

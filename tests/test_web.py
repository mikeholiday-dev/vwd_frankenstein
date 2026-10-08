"""Owner: D."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from channels import web
from channels.credentials import CredentialStore


@pytest.fixture
def app(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    monkeypatch.setattr(web, "store", store)
    return web, store


def test_status_starts_unconfigured(app):
    w, _ = app
    status = w.status()
    assert status.keys() == {"telegram", "elevenlabs", "apify"}
    assert all(s == {"configured": False, "remembered": False} for s in status.values())


def test_set_without_remember_is_configured_but_not_remembered(app):
    w, _ = app
    w.set_credential("elevenlabs", w.SetCredential(value="sk-1", remember=False))
    assert w.status()["elevenlabs"] == {"configured": True, "remembered": False}


def test_set_with_remember_marks_it_remembered(app):
    w, _ = app
    w.set_credential("telegram", w.SetCredential(value="tok", remember=True))
    assert w.status()["telegram"] == {"configured": True, "remembered": True}


def test_the_value_itself_never_comes_back(app):
    w, _ = app
    w.set_credential("apify", w.SetCredential(value="super-secret-token", remember=True))
    for s in w.status().values():
        assert "super-secret-token" not in str(s)


def test_forget_clears_it(app):
    w, _ = app
    w.set_credential("telegram", w.SetCredential(value="tok", remember=True))
    w.forget_credential("telegram")
    assert w.status()["telegram"] == {"configured": False, "remembered": False}


def test_unknown_service_is_a_404(app):
    w, _ = app
    with pytest.raises(HTTPException) as err:
        w.set_credential("not-a-service", w.SetCredential(value="x"))
    assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        w.forget_credential("not-a-service")


def test_index_serves_the_static_page(app):
    w, _ = app
    assert "Frankenstein credentials" in w.index()

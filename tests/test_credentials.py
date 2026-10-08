"""Owner: D."""

from __future__ import annotations

import stat

import pytest

from channels.credentials import CredentialStore, offered_secrets


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path / "credentials.json")


def test_offered_secrets_is_empty_with_no_keys(store):
    assert offered_secrets(store) == {}


def test_offered_secrets_maps_store_keys_to_vault_env_names(store):
    store.set("apify", "apify-tok")
    store.set("elevenlabs", "el-key")
    store.set("telegram", "bot-tok")  # not a vault secret: never offered to the agent
    assert offered_secrets(store) == {"APIFY_TOKEN": "apify-tok", "ELEVENLABS_API_KEY": "el-key"}


def test_unset_service_is_none(store):
    assert store.get("elevenlabs") is None


def test_set_without_remember_is_in_memory_only(store):
    store.set("elevenlabs", "sk-123")
    assert store.get("elevenlabs") == "sk-123"
    assert not store._path.exists()


def test_set_with_remember_persists_outside_the_repo(store):
    store.set("telegram", "bot-token", remember=True)
    assert store._path.is_file()
    assert store._path.read_text().find("bot-token") != -1


def test_persisted_file_is_only_readable_by_the_owner(store):
    store.set("telegram", "bot-token", remember=True)
    mode = stat.S_IMODE(store._path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_a_new_store_over_the_same_path_sees_a_remembered_value(tmp_path):
    path = tmp_path / "credentials.json"
    CredentialStore(path).set("apify", "apify-token", remember=True)
    assert CredentialStore(path).get("apify") == "apify-token"


def test_a_new_store_does_not_see_an_unremembered_value(tmp_path):
    path = tmp_path / "credentials.json"
    CredentialStore(path).set("apify", "apify-token")
    assert CredentialStore(path).get("apify") is None


def test_env_var_overrides_a_remembered_value(store, monkeypatch):
    store.set("elevenlabs", "remembered", remember=True)
    monkeypatch.setenv("FRANK_ELEVENLABS_KEY", "from-env")
    assert store.get("elevenlabs") == "from-env"


def test_forget_clears_both_memory_and_the_persisted_file(store):
    store.set("telegram", "bot-token", remember=True)
    store.forget("telegram")
    assert store.get("telegram") is None
    assert "telegram" not in store._read_persisted()


def test_unknown_service_is_rejected(store):
    with pytest.raises(ValueError):
        store.set("not-a-real-service", "x")
    with pytest.raises(ValueError):
        store.get("not-a-real-service")


def test_remembered_lists_only_persisted_services(store):
    store.set("telegram", "bot-token", remember=True)
    store.set("apify", "apify-token")  # in-memory only
    assert store.remembered() == {"telegram"}

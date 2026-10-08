"""Keys for keyed APIs (plan §3 gateway mode): who may use a key, and where it may go."""

import pytest

from harness import config
from harness.contracts import Permissions
from harness.kernel import vault


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", path)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    return path


def test_secret_reads_the_env_then_dotenv(env_file, monkeypatch):
    env_file.write_text("# comment\nexport APIFY_TOKEN='from-file'\nOTHER=x\n")
    assert config.secret("APIFY_TOKEN") == "from-file"
    monkeypatch.setenv("APIFY_TOKEN", "from-env")
    assert config.secret("APIFY_TOKEN") == "from-env"
    assert config.secret("MISSING") == ""


def test_only_bound_provisioned_and_opted_in_secrets_are_offered(env_file, monkeypatch):
    env_file.write_text("APIFY_TOKEN=k1\nUNBOUND=k2\n")
    assert vault.offered() == []  # a key on the machine isn't enough
    monkeypatch.setattr(config, "SECRETS", ["APIFY_TOKEN", "UNBOUND", "ELEVENLABS_API_KEY"])
    assert vault.offered() == ["APIFY_TOKEN"]  # UNBOUND has no binding, ELEVENLABS_API_KEY no value
    assert vault.for_proxy() == {"APIFY_TOKEN": {"hosts": ("api.apify.com",), "header": "Authorization", "template": "Bearer {}", "value": "k1"}}


def test_problem_names_no_hosts(env_file, monkeypatch):
    env_file.write_text("APIFY_TOKEN=k1\n")
    assert "isn't offered" in vault.problem(Permissions(network=["api.apify.com"], secrets=["APIFY_TOKEN"]))
    monkeypatch.setattr(config, "SECRETS", ["APIFY_TOKEN"])
    assert vault.problem(Permissions(network=["api.apify.com"], secrets=["APIFY_TOKEN"])) == ""
    reason = vault.problem(Permissions(network=["evil.example"], secrets=["APIFY_TOKEN"]))
    assert "never sent" in reason and "apify.com" not in reason
    assert vault.problem(Permissions()) == ""

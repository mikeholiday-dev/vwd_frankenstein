"""Git registry specifics (plan §3, §5): history proves who wrote what, tags are versions."""

import shutil
import subprocess

import pytest
import yaml

from conftest import BUNDLES
from harness.contracts import Manifest
from harness.kernel.registry import GitRegistry


def git(reg, *args):
    return subprocess.run(["git", *args], cwd=reg.root, capture_output=True, text=True, check=True).stdout


def bundle(tmp_path, version, **changes):
    b = tmp_path / f"echo_v{version}"
    shutil.copytree(BUNDLES / "echo_ok", b)
    m = yaml.safe_load((b / "manifest.yaml").read_text())
    m.update(version=version, origin={"task_id": "t-0007", "session": "B"}, **changes)
    (b / "manifest.yaml").write_text(yaml.safe_dump(m))
    return b, Manifest.load(b)


@pytest.fixture
def reg(tmp_path):
    return GitRegistry(tmp_path / "registry")


def test_new_registry_is_an_empty_repo(reg):
    assert reg.list() == []
    assert git(reg, "log", "--format=%an|%s").splitlines() == ["frankenstein-harness|registry created (empty)"]


def test_install_is_an_agent_commit_with_a_tag(reg, tmp_path):
    reg.install(*bundle(tmp_path, 1))

    author, subject, *body = git(reg, "log", "-1", "--format=%an%n%s%n%b").splitlines()
    assert (author, subject) == ("frankenstein-agent", "install echo@v1")
    assert "task: t-0007" in body and "session: B" in body
    assert git(reg, "tag").split() == ["echo@v1"]
    assert git(reg, "status", "--porcelain") == ""


def test_same_version_twice_is_refused(reg, tmp_path):
    reg.install(*bundle(tmp_path, 1))
    with pytest.raises(FileExistsError):
        reg.install(*bundle(tmp_path / "again", 1))


def test_rollback_restores_the_working_tree_and_keeps_v2(reg, tmp_path):
    reg.install(*bundle(tmp_path, 1))
    reg.install(*bundle(tmp_path, 2, description="v2"))
    assert Manifest.load(reg.root / "echo").version == 2

    reg.rollback("echo", 1)

    assert Manifest.load(reg.root / "echo").version == 1
    assert reg.get("echo").manifest.version == 1
    assert reg.get("echo", 2).manifest.description == "v2"
    assert git(reg, "log", "-1", "--format=%an|%s").strip() == "frankenstein-operator|rollback echo to v1"
    with pytest.raises(KeyError):
        reg.rollback("echo", 3)


def test_unknown_name_or_version_raises_key_error(reg, tmp_path):
    reg.install(*bundle(tmp_path, 1))
    with pytest.raises(KeyError):
        reg.get("nope")
    with pytest.raises(KeyError):
        reg.get("echo", 5)


def test_fresh_process_sees_the_same_registry(reg, tmp_path):
    reg.install(*bundle(tmp_path, 1))
    reg.quarantine("echo")

    again = GitRegistry(reg.root)
    assert again.list() == []
    assert [(e.manifest.ref, e.status) for e in again.list(include_quarantined=True)] == [("echo@v1", "quarantined")]


def test_refuses_a_folder_that_is_not_a_git_registry(tmp_path):
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "_state.json").write_text("{}")
    with pytest.raises(RuntimeError, match="isn't a git registry"):
        GitRegistry(tmp_path / "registry")

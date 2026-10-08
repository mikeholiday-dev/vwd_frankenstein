"""Sandbox isolation (plan §6). The `sandbox` fixture runs both fake and Docker; `docker` is real-only."""

import subprocess
from pathlib import Path

import pytest

from harness.contracts import REGISTRY_ENV
from harness.kernel.sandbox import DockerSandbox, DockerUnavailable


@pytest.fixture
def docker(tmp_path):
    try:
        return DockerSandbox(registry_dir=tmp_path / "registry")
    except DockerUnavailable as e:
        pytest.skip(f"no docker here: {e}")


def py(code):
    return ["python", "-c", code]


def test_timeout_kills_the_run(sandbox, tmp_path):
    r = sandbox.run(tmp_path, py("import time; time.sleep(30)"), phase="test", timeout_s=2)
    assert r.timed_out and r.exit_code != 0
    assert r.duration_s < 15


def test_deps_are_importable(sandbox, tmp_path):
    r = sandbox.run(tmp_path, py("import six; print(six.__version__)"), phase="test", deps=["six==1.16.0"])
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "1.16.0"


def test_timed_out_container_is_removed(docker, tmp_path):
    r = docker.run(tmp_path, py("import time; time.sleep(30)"), phase="call", timeout_s=2)
    assert r.timed_out
    ps = subprocess.run([docker.docker, "ps", "-aq", "--filter", f"name=frank-call-{r.run_id}"], capture_output=True, text=True, env=docker._cli_env)
    assert ps.stdout.strip() == ""


def test_no_network(docker, tmp_path):
    r = docker.run(tmp_path, py("import socket; socket.create_connection(('1.1.1.1', 443), 3)"), phase="test", network=["example.org"])
    assert r.exit_code != 0


def test_no_host_env_home_or_files(docker, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    probe = "import os, pathlib; print(sorted(os.environ)); print(os.environ['HOME']); print(pathlib.Path(%r).exists())" % str(Path.home())
    r = docker.run(tmp_path, py(probe), phase="call")
    env, home, host_home_visible = r.stdout.splitlines()
    assert "ANTHROPIC_API_KEY" not in env
    assert home == "/tmp"
    assert host_home_visible == "False"


def test_call_phase_mounts_workdir_read_only(docker, tmp_path):
    write = py("open('out.txt', 'w').write('x')")
    assert docker.run(tmp_path, write, phase="call").exit_code != 0
    assert docker.run(tmp_path, write, phase="test").exit_code == 0
    assert (tmp_path / "out.txt").read_text() == "x"


def test_registry_is_mounted_read_only(docker, tmp_path):
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "hello.txt").write_text("hi")
    read = py(f"import os; print(open(os.environ['{REGISTRY_ENV}'] + '/hello.txt').read())")
    assert docker.run(tmp_path, read, phase="call", registry_ro=True).stdout.strip() == "hi"
    assert docker.run(tmp_path, read, phase="call").exit_code != 0
    write = py(f"import os; open(os.environ['{REGISTRY_ENV}'] + '/x', 'w')")
    assert docker.run(tmp_path, write, phase="call", registry_ro=True).exit_code != 0


def test_option_like_deps_are_refused(docker, tmp_path):
    r = docker.run(tmp_path, ["true"], phase="test", deps=["--index-url=http://evil.example"])
    assert r.exit_code != 0 and "refused" in r.stderr


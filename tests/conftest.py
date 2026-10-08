from pathlib import Path

import pytest

from harness import fakes
from harness.kernel.registry import GitRegistry
from harness.kernel.sandbox import DockerSandbox
from harness.ops.events import EventLog

BUNDLES = Path(__file__).parent / "fixtures" / "bundles"


@pytest.fixture
def events(tmp_path):
    return EventLog(tmp_path / "events.jsonl", session="test")


# Every kernel test runs against the fake AND the real implementation. The
# real one is skipped until its owner removes the NotImplementedError, then
# the same tests become its acceptance check.


@pytest.fixture(params=["local", "docker"])
def sandbox(request, tmp_path):
    sb = fakes.LocalSandbox() if request.param == "local" else DockerSandbox()
    try:
        sb.run(tmp_path, ["true"], phase="call")
    except NotImplementedError:
        pytest.skip(f"{type(sb).__name__} not implemented yet")
    return sb


@pytest.fixture(params=["dir", "git"])
def registry(request, tmp_path):
    reg = fakes.DirRegistry(tmp_path / "registry") if request.param == "dir" else GitRegistry(tmp_path / "registry")
    try:
        reg.list()
    except NotImplementedError:
        pytest.skip(f"{type(reg).__name__} not implemented yet")
    return reg

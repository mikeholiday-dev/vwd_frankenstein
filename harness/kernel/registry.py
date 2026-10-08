"""Git-backed registry (plan §3, §5). Owner: A.

Implements contracts.Registry. Until it's done, wiring uses fakes.DirRegistry,
whose behaviour (and tests/test_install_flow.py) is the spec.

Layout: `registry/` is its own git repo. One folder per capability, one commit
per install, tag `<name>@v<N>` per version. Status (active version, quarantined)
lives in a tracked `_state.json` so it's versioned too.
- get(name, version): materialise that tag's folder into a cache dir (git archive / worktree)
- rollback: point active at an older tag, commit, emit nothing (the caller logs)
- commit author: "frankenstein-agent", message includes origin.task_id + session,
  so `git log` proves the team didn't write it
- raise KeyError for unknown names/versions (the gate relies on it)
"""

from __future__ import annotations

from pathlib import Path


class GitRegistry:
    def __init__(self, root: Path):
        self.root = root

    def list(self, include_quarantined=False):
        raise NotImplementedError("stream A")

    def get(self, name, version=None):
        raise NotImplementedError("stream A")

    def install(self, bundle_dir, manifest):
        raise NotImplementedError("stream A")

    def rollback(self, name, version):
        raise NotImplementedError("stream A")

    def quarantine(self, name):
        raise NotImplementedError("stream A")

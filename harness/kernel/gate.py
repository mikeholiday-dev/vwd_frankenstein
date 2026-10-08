"""Install gate (plan §5). Owner: A.

The only path into the registry. It snapshots the bundle out of the agent's
workspace, checks it, runs the tests itself in a fresh sandbox, logs the full
output, asks the operator, and installs. It never trusts a pass/fail claim from the agent.

- refused before any test runs: an invalid manifest, a name that isn't a plain identifier,
  symlinks (on the host they'd point at host files), a missing capability.py or tests/, and a
  version that's already installed or skips one (a new name starts at v1)
- tests run on a throwaway copy, so what's installed is exactly the snapshot the operator saw
- v2+: the active version's stored tests also run against the candidate; both suites must pass
- `uses` (compose.py): every capability it reaches must be installed, active, acyclic and need no
  permission the bundle doesn't declare itself; they're copied next to the code for every test run.
  A bundle's own copy of the harness-written `frank.py` / `_frank_uses/` is dropped from the snapshot
- `run_tests` is shared with Host.retest, the "re-run the stored tests" primitive for capability_doctor

TODO(A): prompt_skill: run eval cases + LLM judge instead of pytest. Refused until then.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import uuid
from pathlib import Path

from harness.contracts import (
    CODE_FILE,
    TEST_ARGV,
    TESTS_DIR,
    ApprovalRequest,
    Approver,
    EventType,
    InstallResult,
    Kind,
    Manifest,
    Phase,
    Registry,
    RegistryEntry,
    Sandbox,
    TestReport,
    permissions_diff,
)
from harness.kernel.compose import RESERVED, UsesError, dependencies, resolve, vendor
from harness.ops.events import EventLog

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def run_tests(
    sandbox: Sandbox, bundle: Path, manifest: Manifest, events: EventLog, suite: str = "own", uses: list[RegistryEntry] = ()
) -> TestReport:
    """Run `bundle`'s tests in the sandbox on a throwaway copy, with its resolved `uses` alongside, and log the full output."""
    with tempfile.TemporaryDirectory(prefix=f"test-{manifest.name}-") as tmp:
        work = Path(tmp) / "bundle"
        shutil.copytree(bundle, work, symlinks=True)
        vendor(list(uses), work)
        r = sandbox.run(
            work, TEST_ARGV, phase=Phase.TEST, network=manifest.permissions.network, deps=dependencies(manifest, list(uses)),
            registry_ro=manifest.permissions.filesystem == "registry_ro",
        )
    report = TestReport(
        ref=manifest.ref,
        passed=r.exit_code == 0 and not r.timed_out,
        exit_code=r.exit_code,
        output=r.stdout + r.stderr + ("\n[timed out]" if r.timed_out else ""),
        duration_s=round(r.duration_s, 2),
        sandbox_run_id=r.run_id,
    )
    events.emit(EventType.TEST_RUN, **vars(report), suite=suite, egress_denied=r.egress_denied)
    return report


class Gate:
    def __init__(self, sandbox: Sandbox, registry: Registry, approver: Approver, events: EventLog):
        self.sandbox, self.registry, self.approver, self.events = sandbox, registry, approver, events

    def submit(self, bundle_dir: Path) -> InstallResult:
        with tempfile.TemporaryDirectory(prefix="gate-") as tmp:
            candidate = Path(tmp) / "bundle"
            shutil.copytree(bundle_dir, candidate, symlinks=True)  # snapshot: the agent can't change it mid-run
            for r in RESERVED:
                if (candidate / r).is_dir() and not (candidate / r).is_symlink():
                    shutil.rmtree(candidate / r)
                else:
                    (candidate / r).unlink(missing_ok=True)
            try:
                manifest = Manifest.load(candidate)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                return self._refused(Path(bundle_dir).name, f"invalid manifest: {type(e).__name__}: {e}")
            if reason := self._precheck(candidate, manifest):
                return self._refused(manifest.ref, reason)
            try:
                uses = resolve(self.registry, manifest)
            except UsesError as e:
                return self._refused(manifest.ref, str(e))

            previous = self._active(manifest.name)
            report = run_tests(self.sandbox, candidate, manifest, self.events, uses=uses)
            if report.passed and previous:
                report = _combined(report, self._regression(candidate, manifest, previous, uses))
            if not report.passed:
                return self._result(manifest.ref, False, "tests failed", report)

            req = ApprovalRequest(
                id=uuid.uuid4().hex[:8],
                ref=manifest.ref,
                manifest=manifest.to_dict(),
                previous=previous.manifest.to_dict() if previous else None,
                permissions_diff=permissions_diff(previous.manifest.permissions if previous else None, manifest.permissions),
                test_report=report,
                code=(candidate / CODE_FILE).read_text() if (candidate / CODE_FILE).exists() else "",
            )
            decision = self.approver.request(req)
            if not decision.approved:
                return self._result(manifest.ref, False, f"rejected by {decision.by}: {decision.reason}", report)

            self.registry.install(candidate, manifest)
            return self._result(manifest.ref, True, f"approved by {decision.by}", report)

    def _precheck(self, bundle: Path, manifest: Manifest) -> str:
        """Why the bundle can't be installed whatever its tests say, or "" if it can."""
        if not NAME_RE.fullmatch(str(manifest.name)):
            return f"name {manifest.name!r} must be lowercase letters, digits and _ (max 64)"
        if not isinstance(manifest.version, int) or isinstance(manifest.version, bool) or manifest.version < 1:
            return f"version must be a positive integer, got {manifest.version!r}"
        if links := [str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_symlink()]:
            return f"bundle contains symlinks: {links}"
        if manifest.kind != Kind.CODE_TOOL:
            return f"{manifest.kind} installs aren't supported yet"
        if not (bundle / CODE_FILE).is_file():
            return f"missing {CODE_FILE}"
        if not any((bundle / TESTS_DIR).glob("test_*.py")):
            return f"no {TESTS_DIR}/test_*.py: nothing gets installed without tests"
        installed = self._versions(manifest.name)
        if manifest.version in installed:
            return f"{manifest.ref} is already installed; bump the version"
        expected = max(installed, default=0) + 1
        if manifest.version != expected:
            return f"version must be {expected} (installed: {sorted(installed) or 'none'})"
        return ""

    def _regression(self, candidate: Path, manifest: Manifest, previous: RegistryEntry, uses: list[RegistryEntry]) -> TestReport:
        """The candidate's code against the active version's stored tests."""
        with tempfile.TemporaryDirectory(prefix=f"regress-{manifest.name}-") as tmp:
            work = Path(tmp) / "bundle"
            shutil.copytree(candidate, work, symlinks=True, ignore=lambda d, names: [TESTS_DIR] if Path(d) == candidate else [])
            shutil.copytree(previous.path / TESTS_DIR, work / TESTS_DIR)
            return run_tests(self.sandbox, work, manifest, self.events, suite=f"regression: {previous.manifest.ref} tests", uses=uses)

    def _versions(self, name: str) -> set[int]:
        versions, v = set(), 1
        try:
            active = self.registry.get(name).manifest.version
        except KeyError:
            return versions
        while True:  # installed versions are contiguous: the gate never lets one be skipped
            try:
                self.registry.get(name, v)
            except (KeyError, FileNotFoundError):  # fakes.DirRegistry raises the latter for an unknown version
                if v > active:
                    return versions
            else:
                versions.add(v)
            v += 1

    def _active(self, name: str) -> RegistryEntry | None:
        try:
            return self.registry.get(name)
        except KeyError:
            return None

    def _refused(self, ref: str, reason: str) -> InstallResult:
        return self._result(ref, False, f"refused: {reason}", None)

    def _result(self, ref: str, installed: bool, reason: str, report: TestReport | None) -> InstallResult:
        self.events.emit(EventType.INSTALL, ref=ref, installed=installed, reason=reason)
        return InstallResult(ref, installed, reason, report)


def _combined(own: TestReport, regression: TestReport) -> TestReport:
    """One report for the approval card: the candidate's own tests, then the previous version's."""
    return TestReport(
        ref=own.ref,
        passed=own.passed and regression.passed,
        exit_code=own.exit_code or regression.exit_code,
        output=f"=== own tests ===\n{own.output}\n=== previous version's tests ===\n{regression.output}",
        duration_s=round(own.duration_s + regression.duration_s, 2),
        sandbox_run_id=f"{own.sandbox_run_id},{regression.sandbox_run_id}",
    )

"""Install gate (plan §5). Owner: A.

The only path into the registry. It copies the bundle out of the agent's
workspace, runs the tests itself in a fresh sandbox, logs the full output, asks
the operator, and installs. It never trusts a pass/fail claim from the agent.

TODO(A):
- v2+: copy the previous version's tests into the candidate and require them to pass too.
- prompt_skill: run eval cases + LLM judge instead of pytest.
- refuse a bundle whose manifest name/version clashes or skips a version.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from harness.contracts import (
    CODE_FILE,
    TEST_ARGV,
    ApprovalRequest,
    EventType,
    InstallResult,
    Manifest,
    Phase,
    Registry,
    Sandbox,
    Approver,
    TestReport,
    permissions_diff,
)
from harness.ops.events import EventLog


class Gate:
    def __init__(self, sandbox: Sandbox, registry: Registry, approver: Approver, events: EventLog):
        self.sandbox, self.registry, self.approver, self.events = sandbox, registry, approver, events

    def submit(self, bundle_dir: Path) -> InstallResult:
        manifest = Manifest.load(bundle_dir)
        with tempfile.TemporaryDirectory(prefix=f"gate-{manifest.name}-") as tmp:
            candidate = Path(tmp) / "bundle"
            shutil.copytree(bundle_dir, candidate)  # snapshot: the agent can't change it mid-run

            report = self._test(candidate, manifest)
            if not report.passed:
                return self._result(manifest, False, "tests failed", report)

            previous = self._active(manifest.name)
            req = ApprovalRequest(
                id=uuid.uuid4().hex[:8],
                ref=manifest.ref,
                manifest=manifest.to_dict(),
                previous=previous.to_dict() if previous else None,
                permissions_diff=permissions_diff(previous.permissions if previous else None, manifest.permissions),
                test_report=report,
                code=(candidate / CODE_FILE).read_text() if (candidate / CODE_FILE).exists() else "",
            )
            decision = self.approver.request(req)
            if not decision.approved:
                return self._result(manifest, False, f"rejected by {decision.by}: {decision.reason}", report)

            self.registry.install(candidate, manifest)
            return self._result(manifest, True, f"approved by {decision.by}", report)

    def _test(self, bundle: Path, manifest: Manifest) -> TestReport:
        r = self.sandbox.run(bundle, TEST_ARGV, phase=Phase.TEST, network=manifest.permissions.network, deps=manifest.dependencies)
        report = TestReport(
            ref=manifest.ref,
            passed=r.exit_code == 0 and not r.timed_out,
            exit_code=r.exit_code,
            output=r.stdout + r.stderr + ("\n[timed out]" if r.timed_out else ""),
            duration_s=round(r.duration_s, 2),
            sandbox_run_id=r.run_id,
        )
        self.events.emit(EventType.TEST_RUN, **vars(report))
        return report

    def _active(self, name: str) -> Manifest | None:
        try:
            return self.registry.get(name).manifest
        except KeyError:
            return None

    def _result(self, manifest: Manifest, installed: bool, reason: str, report: TestReport) -> InstallResult:
        self.events.emit(EventType.INSTALL, ref=manifest.ref, installed=installed, reason=reason)
        return InstallResult(manifest.ref, installed, reason, report)

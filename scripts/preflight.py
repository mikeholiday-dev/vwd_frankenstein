"""Check this machine is ready for a rehearsal or the recorded take. Owner: C.

    uv run python scripts/preflight.py          # exit code 1 if anything blocks a clean run
    uv run python scripts/preflight.py --offline

Read-only: it starts nothing and changes nothing. Every line is OK, WARN (rehearse anyway, fix
before recording) or FAIL (the run would be wrong or refused).
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys

from harness import config

# Hosts the demo tasks reach in the real world; the team knows them from the README API check.
DEMO_HOSTS = ["ares.gov.cz", "mojedane.gov.cz", "www.cnb.cz"]


def checks(offline: bool = False, env: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    env = os.environ if env is None else env
    out: list[tuple[str, str, str]] = []

    def add(level: str, what: str, detail: str = "") -> None:
        out.append((level, what, detail))

    mode, fakes = env.get("FRANK_MODE", "dev"), env.get("FRANK_FAKE", "none")
    add("OK" if mode == "demo" else "WARN", f"FRANK_MODE={mode}", "" if mode == "demo" else "export FRANK_MODE=demo for the recorded run (it refuses fakes and the auto approver)")
    add("OK" if fakes in ("", "none") else "FAIL", f"FRANK_FAKE={fakes}", "" if fakes in ("", "none") else "fakes run generated code on this machine or skip the git registry")
    approver = env.get("FRANK_APPROVER", "cli")
    add("OK" if approver == "ui" else "WARN", f"FRANK_APPROVER={approver}", "" if approver == "ui" else "use ui so the operator approves on the dashboard card")
    if env.get("ANTHROPIC_API_KEY"):
        add("FAIL", "ANTHROPIC_API_KEY is set", "unset it: the Agent SDK would use and bill it instead of the subscription")
    else:
        add("OK", "ANTHROPIC_API_KEY is unset", f"auth: {config.AUTH}")
    if not shutil.which("claude"):
        add("WARN", "`claude` CLI not found on PATH", "the Agent SDK needs a Claude Code login (claude, then /login)")

    docker = shutil.which("docker")
    if not docker:
        add("FAIL", "docker CLI not found", "install Docker Desktop")
    else:
        info = subprocess.run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=30)
        if info.returncode:
            add("FAIL", "docker daemon not reachable", "start Docker Desktop")
        else:
            add("OK", f"docker daemon {info.stdout.strip()}")
            images = subprocess.run([docker, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True, timeout=30).stdout
            built = any("frank-sandbox" in line for line in images.splitlines())
            add("OK" if built else "WARN", "sandbox image " + ("built" if built else "not built yet"),
                "" if built else "the first sandbox run builds it (~30 s, needs internet): do that before the take")

    reg, log, work = config.REGISTRY_DIR, config.LOG_PATH, config.WORK_DIR
    if reg.exists() and any(reg.iterdir()):
        commits = subprocess.run(["git", "-C", str(reg), "rev-list", "--count", "HEAD"], capture_output=True, text=True)
        add("WARN", f"registry {reg} is not empty", f"{commits.stdout.strip() or 'no'} commits: scripts/fresh_start.py moves it aside (storyboard 0-8 s needs it empty)")
    else:
        add("OK", "registry is empty or absent")
    size = log.stat().st_size if log.exists() else 0
    add("OK" if size == 0 else "WARN", f"event log {log} " + ("is empty" if size == 0 else f"holds {size} bytes"),
        "" if size == 0 else "an old run would show on the dashboard: scripts/fresh_start.py")
    if work.exists() and any(work.iterdir()):
        add("WARN", f"work folder {work} has old build workspaces", "scripts/fresh_start.py moves it aside")

    port = int(env.get("FRANK_CREDENTIALS_PORT", "8001"))
    with socket.socket() as s:
        s.settimeout(1)
        up = s.connect_ex(("127.0.0.1", port)) == 0
    add("OK" if up else "WARN", f"dashboard {'is up' if up else 'is not running'} on :{port}", "" if up else "uv run python -m channels.run")

    for name in ("testdata/invoice_ok.pdf", "testdata/invoice_bad_account.pdf", "testdata/tasks.md"):
        add("OK" if (config.ROOT / name).is_file() else "FAIL", name)
    if offline:
        return out
    for host in DEMO_HOSTS:
        try:
            socket.create_connection((host, 443), timeout=5).close()
            add("OK", f"{host}:443 reachable")
        except OSError as e:
            add("WARN", f"{host}:443 not reachable", f"{type(e).__name__}: the live take needs it")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--offline", action="store_true", help="skip the network checks")
    a = p.parse_args(argv)
    rows = checks(a.offline)
    for level, what, detail in rows:
        print(f"{level:4} {what}" + (f"  ({detail})" if detail else ""))
    fails = sum(level == "FAIL" for level, _, _ in rows)
    warns = sum(level == "WARN" for level, _, _ in rows)
    print(f"\n{fails} blocking, {warns} to look at")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

"""Kernel tools the agent gets (plan §4, §7). Owner: B.

Plain functions over a Context; loop.py wraps them as Agent SDK tools. Keep the
set small and dumb: the agent has no network of its own, so task data can only
come from capability calls, and the discovery/management tooling is something
the agent builds itself on top of these primitives.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from harness import config
from harness.contracts import CODE_FILE, TESTS_DIR, EventType, Phase, to_jsonable
from harness.wiring import Context


def registry_list(ctx: Context) -> list[dict[str, Any]]:
    """Raw manifests of active capabilities. No search, ranking or health on purpose."""
    return [e.manifest.to_dict() for e in ctx.registry.list()]


def registry_read(ctx: Context, name: str, version: int | None = None) -> dict[str, Any]:
    e = ctx.registry.get(name, version)
    tests = {p.name: p.read_text() for p in sorted((e.path / TESTS_DIR).glob("*.py"))}
    code = (e.path / CODE_FILE).read_text() if (e.path / CODE_FILE).exists() else ""
    return {"manifest": e.manifest.to_dict(), "status": e.status, "code": code, "tests": tests}


def write_file(ctx: Context, path: str, content: str) -> str:
    """Write inside this run's build workspace only."""
    target = _in_workspace(ctx, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return str(target.relative_to(ctx.workdir.resolve()))  # the workdir path may run through a symlink (/tmp on macOS)


def sandbox_exec(ctx: Context, bundle: str, argv: list[str], deps: list[str] | None = None) -> dict[str, Any]:
    """Try things while building (build phase: package index only). Doesn't count as the install test run."""
    r = ctx.sandbox.run(_in_workspace(ctx, bundle), argv, phase=Phase.BUILD, deps=deps or [])
    return to_jsonable(r)


def registry_install(ctx: Context, bundle: str) -> dict[str, Any]:
    """Hand a bundle to the install gate. The gate runs the tests itself and asks the operator."""
    return to_jsonable(ctx.gate.submit(_in_workspace(ctx, bundle)))


def invoke_capability(ctx: Context, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Generic fallback if hot-loading tools mid-session misbehaves (plan §13)."""
    return to_jsonable(ctx.host.call(name, args))


def registry_retest(ctx: Context, name: str, version: int | None = None) -> dict[str, Any]:
    """Re-run an installed version's stored tests in the sandbox. The harness runs them, and logs the output."""
    retest = getattr(ctx.host, "retest", None)  # on kernel.host.Host, not yet on the CapabilityHost Protocol
    if retest is None:
        raise NotImplementedError("this capability host can't re-run stored tests")
    return to_jsonable(retest(name, version))


def registry_propose_rollback(ctx: Context, name: str, version: int) -> dict[str, Any]:
    """TODO(B, with C): emit a proposal the operator confirms in the UI; the UI does the rollback."""
    raise NotImplementedError("stream B")


def read_file(ctx: Context, path: str) -> str:
    """Read a text file from this run's build workspace."""
    return _in_workspace(ctx, path).read_text()[:STUDY_MAX_CHARS]


def list_files(ctx: Context, path: str = ".") -> list[str]:
    root = _in_workspace(ctx, path)
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


STUDY_MAX_CHARS = 20_000
STUDY_MAX_BYTES = 2_000_000
STUDY_TIMEOUT_S = 15
SEARCH_URL = "https://html.duckduckgo.com/html/?q={q}"
# With APIFY_TOKEN set (env or .env), searches go through Apify's Google Search actor: the
# token stays on the host, and the agent gets the same title + URL list either way.
APIFY_SEARCH_URL = "https://api.apify.com/v2/acts/apify~google-search-scraper/run-sync-get-dataset-items?timeout=60"
APIFY_TIMEOUT_S = 90
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml", "application/javascript", "application/yaml")


def study(ctx: Context, query: str, url: str | None = None) -> str:
    """Search the web for `query`, or read `url`. GET only, text only, size-capped, logged as a STUDY event.

    Runs on the host (it's team code, not generated code) but must not become a
    data channel: the provenance check flags answers without capability calls.
    """
    backend = "page" if url else "apify" if config.secret("APIFY_TOKEN") else "duckduckgo"
    try:
        if backend == "apify":
            text = _apify_search(query, config.secret("APIFY_TOKEN"))
        else:
            text = _fetch_text(url or SEARCH_URL.format(q=urllib.parse.quote_plus(query)))
            if not url:
                text = _search_results(text) or text
        ok = True
    except (OSError, ValueError) as e:
        text, ok = f"study failed: {type(e).__name__}: {e}", False
    text = text[:STUDY_MAX_CHARS]
    ctx.events.emit(EventType.STUDY, query=query, url=url or "", ok=ok, chars=len(text), backend=backend)
    return text


def _apify_search(query: str, token: str) -> str:
    """Title + URL lines, like _search_results. No snippets: they would carry page text, not just where to look."""
    body = json.dumps({"queries": query, "maxPagesPerQuery": 1}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(APIFY_SEARCH_URL, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=APIFY_TIMEOUT_S) as r:
        pages = json.loads(r.read(STUDY_MAX_BYTES))
    seen, rows = set(), []
    for page in pages:
        for hit in page.get("organicResults") or []:
            target, label = hit.get("url", ""), " ".join(str(hit.get("title", "")).split())
            if target.startswith("http") and label and target not in seen:
                seen.add(target)
                rows.append(f"{label}\n  {target}")
    if not rows:
        raise ValueError("search returned no results")
    return "\n".join(rows[:15])


def _fetch_text(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs can be studied")
    for info in socket.getaddrinfo(parts.hostname, None):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"{parts.hostname} is not a public address")
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0 (frankenstein study; docs reader)", "Accept": "text/*, application/json"})
    with urllib.request.urlopen(req, timeout=STUDY_TIMEOUT_S) as r:
        ctype = r.headers.get_content_type()
        if not ctype.startswith(_TEXT_TYPES) and "xml" not in ctype and "json" not in ctype:
            raise ValueError(f"not text: {ctype}")
        raw = r.read(STUDY_MAX_BYTES)
        text = raw.decode(r.headers.get_content_charset() or "utf-8", errors="replace")
    return _html_to_text(text) if "html" in ctype else text


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre", "section", "table"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self._href, self._label = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._label).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._label.append(data)


def _html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    lines = (" ".join(line.split()) for line in "".join(p.out).splitlines())
    return "\n".join(line for line in lines if line)


def _search_results(html: str) -> str:
    """Title + URL lines from a search results page, so the agent can pick a page to read next."""
    p = _Text()
    p.feed(html)
    seen, rows = set(), []
    for href, label in p.links:
        target = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [href])[0]
        if target.startswith("http") and "duckduckgo.com" not in target and label and target not in seen:
            seen.add(target)
            rows.append(f"{label}\n  {target}")
    return "\n".join(rows[:15])


def _in_workspace(ctx: Context, path: str) -> Path:
    target = (ctx.workdir / path).resolve()
    if not target.is_relative_to(ctx.workdir.resolve()):
        raise PermissionError(f"{path} is outside the build workspace")
    return target

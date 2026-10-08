"""Brief rule: the gap comes from the task. No tool names or API endpoints in any prompt.

Owner: everyone. If you add a prompt file anywhere under harness/agent/, it's scanned.
"""

import re
from pathlib import Path

import pytest

AGENT = Path(__file__).parent.parent / "harness" / "agent"

BANNED = [
    # hosts and endpoints from the README API check
    r"ares\.gov", r"\bares\b", r"mojedane", r"adisrws", r"mfcr", r"cnb\.cz", r"denni_kurz", r"kurzy", r"rozhraniCRPDPH",
    r"ekonomicke-subjekty", r"NespolehlivyPlatce", r"wsdl", r"zeep",
    # capability names the agent is expected to come up with itself
    r"ares_lookup", r"vat_payer_status", r"parse_invoice", r"cnb_rate", r"find_capability", r"capability_report", r"capability_doctor",
]

PROMPTS = sorted(p for p in AGENT.rglob("*") if p.suffix in {".md", ".txt", ".j2"}) + sorted(AGENT.rglob("*prompt*.py"))


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: str(p.relative_to(AGENT)))
def test_prompt_names_no_tools_or_endpoints(path):
    text = path.read_text()
    hits = [b for b in BANNED if re.search(b, text, re.IGNORECASE)]
    assert not hits, f"{path.name} leaks {hits}"

"""Owner: D. Pure, platform-agnostic helpers: no Telegram or Discord object touches this file."""

from __future__ import annotations

from channels.chat import approval_callback_data, is_busy, parse_approval_callback, quality_for, truncate


def test_quality_defaults_to_cheap():
    assert quality_for({}) == "cheap"
    assert quality_for({"quality": "full"}) == "full"


def test_is_busy():
    assert not is_busy({})
    assert not is_busy({"busy": False})
    assert is_busy({"busy": True})


def test_approval_callback_round_trips():
    data = approval_callback_data("req-9", True)
    assert parse_approval_callback(data) == ("req-9", True)
    data = approval_callback_data("req-9", False)
    assert parse_approval_callback(data) == ("req-9", False)


def test_truncate_leaves_short_text_alone():
    assert truncate("short", limit=100) == "short"


def test_truncate_cuts_long_text():
    out = truncate("x" * 5000, limit=100)
    assert len(out) == 100 and out.endswith("…")


def test_truncate_default_limit():
    assert truncate("x" * 2500) != "x" * 2500

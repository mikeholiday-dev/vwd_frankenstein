"""Owner: D. No real Apify token: httpx is monkeypatched, nothing goes over the network."""

from __future__ import annotations

import httpx

from channels import apify


def test_run_actor_posts_input_and_the_token_then_returns_the_items(monkeypatch):
    captured = {}

    def fake_post(url, *, params, json, timeout):
        captured.update(url=url, params=params, json=json, timeout=timeout)
        return httpx.Response(200, json=[{"result": "ok"}], request=httpx.Request("POST", url))

    monkeypatch.setattr(apify.httpx, "post", fake_post)
    items = apify.run_actor("some~actor", {"q": "czech invoices"}, token="tok")

    assert items == [{"result": "ok"}]
    assert captured["url"] == "https://api.apify.com/v2/acts/some~actor/run-sync-get-dataset-items"
    assert captured["params"] == {"token": "tok"}
    assert captured["json"] == {"q": "czech invoices"}


def test_run_actor_raises_on_an_error_response(monkeypatch):
    def fake_post(url, *, params, json, timeout):
        return httpx.Response(401, json={"error": "nope"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(apify.httpx, "post", fake_post)
    try:
        apify.run_actor("a", {}, token="bad")
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("expected an HTTPStatusError")

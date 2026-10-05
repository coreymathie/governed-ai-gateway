# Corey Mathie, 2026
"""Semantic cache: embedder, guards, partitions, TTL/LRU, calibration (bundled fictional pairs), and the API path."""

import json
import textwrap
from pathlib import Path

import litellm
import pytest
from fastapi.testclient import TestClient

from router import config_loader, main, metrics, semcache
from router.config_loader import settings
from router.models import RouterConfig
from scripts import calibrate_semcache

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "evals" / "semcache_pairs.jsonl"
ADMIN = {"Authorization": "Bearer sk-router-admin"}


# ---------- embedder and guards ----------


def test_hashing_embedder_is_deterministic_and_normalised():
    e = semcache.HashingEmbedder()
    a, b = e.embed("How do I reset my password?"), e.embed("How do I reset my password?")
    assert a == b and len(a) == 1024
    assert sum(x * x for x in a) == pytest.approx(1.0)
    assert semcache.cosine(a, e.embed("how do i reset my password")) == pytest.approx(1.0)
    assert semcache.cosine(a, e.embed("Which city hosts the marathon?")) < 0.3
    assert e.embed("") == [0.0] * 1024 and semcache.cosine(e.embed(""), a) == 0.0
    with pytest.raises(ValueError):
        semcache.cosine([1.0], [1.0, 0.0])


def test_guards_numbers_and_negation():
    assert semcache.guard("Convert 5 km to miles", "Convert 7 km to miles") == "numbers differ"
    assert semcache.guard("Refund 1,200 dollars", "Refund 1200 dollars") is None
    assert semcache.guard("Does the app work offline?", "Why doesn't the app work offline?") == "negation differs"
    assert semcache.guard("I never got it", "I did not get it") is None  # both negated
    assert semcache.guard("Convert 5 km", "Convert 7 km", check_numbers=False) is None


# ---------- cache mechanics ----------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_lookup_threshold_guard_partition_ttl_and_lru():
    e, clock = semcache.HashingEmbedder(), Clock()
    c = semcache.SemanticCache(threshold=0.85, ttl_seconds=60, max_entries=2, clock=clock)
    q = "How do I export my invoices as CSV?"
    c.put("team-a", q, e.embed(q), "answer-1")
    hit = c.lookup("team-a", "how do I export my invoices as csv", e.embed("how do I export my invoices as csv"))
    assert hit.hit and hit.entry.value == "answer-1" and hit.reason == "hit"
    assert c.lookup("team-b", q, e.embed(q)).reason == "empty"  # partitions never mix
    far = c.lookup("team-a", "What is the capital of Austria?", e.embed("What is the capital of Austria?"))
    assert not far.hit and far.reason == "below_threshold"
    n, m = "Is the 2025 model compatible with the charger?", "Is the 2023 model compatible with the charger?"
    c.put("team-a", n, e.embed(n), "2025 answer")
    g = c.lookup("team-a", m, e.embed(m))
    assert not g.hit and g.reason == "guard: numbers differ" and g.similarity > 0.85
    c.put("team-a", "third entry", e.embed("third entry"), "x")  # evicts the least recently used
    assert c.size() == 2
    clock.t = 61
    assert c.lookup("team-a", n, e.embed(n)).reason == "empty"  # expired


# ---------- calibration ----------


def test_wilson_bound_and_pick_rules():
    assert semcache.wilson_upper(0, 0) == 1.0
    assert semcache.wilson_upper(0, 10) == pytest.approx(0.2775, abs=1e-3)
    assert semcache.wilson_upper(5, 10) == pytest.approx(0.7634, abs=1e-3)
    rows = [
        {"threshold": 0.7, "hits": 20, "false_hit_rate": 0.0, "false_hit_rate_upper95": 0.16},  # isolated dip
        {"threshold": 0.8, "hits": 15, "false_hit_rate": 0.2, "false_hit_rate_upper95": 0.4},
        {"threshold": 0.9, "hits": 10, "false_hit_rate": 0.0, "false_hit_rate_upper95": 0.28},
        {"threshold": 0.95, "hits": 0, "false_hit_rate": 0.0, "false_hit_rate_upper95": 1.0},
    ]
    assert semcache.pick_threshold(rows, 0.01)["threshold"] == 0.9  # the dip at 0.7 doesn't count
    assert semcache.pick_threshold(rows, 0.01, use_upper_bound=True) is None
    assert semcache.pick_threshold(rows, 0.3, use_upper_bound=True)["threshold"] == 0.9


def test_bundled_pairs_calibration_numbers():
    """The figures quoted in the README: bundled fictional pairs, hashing-v1 embedder, target 1%."""
    pairs = semcache.load_pairs(PAIRS.read_text().splitlines())
    assert len(pairs) == 160 and sum(p.same for p in pairs) == 80
    rep = calibrate_semcache.run(str(PAIRS), 0.01, 0.50, 0.99, 0.01)
    assert rep["pairs"] == {"total": 160, "calibration": 80, "holdout": 80}
    assert rep["chosen"]["threshold"] == 0.88
    assert (rep["chosen"]["hits"], rep["chosen"]["false_hits"], rep["chosen"]["hit_rate"]) == (13, 0, 0.3095)
    h, g = rep["holdout_at_chosen"], rep["holdout_without_guards_at_chosen"]
    assert (h["true_hits"], h["hits"], h["hit_rate"], h["false_hit_rate"]) == (10, 11, 0.2632, 0.0909)
    assert (g["false_hits"], g["hits"], g["false_hit_rate"]) == (4, 14, 0.2857)
    assert calibrate_semcache.run(str(PAIRS), 0.01, 0.5, 0.99, 0.01, conservative=True)["chosen"] is None


def test_calibration_script_outputs(tmp_path, capsys):
    assert calibrate_semcache.main(["--out", str(tmp_path / "cal")]) == 0
    md = (tmp_path / "cal.md").read_text()
    assert "0.88 **(chosen)**" in md and "Holdout at 0.88" in md and "not a prediction for production" in md
    assert json.loads((tmp_path / "cal.json").read_text())["embedder"] == "hashing-v1"
    assert calibrate_semcache.main(["--conservative"]) == 1
    with pytest.raises(ValueError):
        semcache.load_pairs(['{"id": "x", "a": "1", "b": "2", "same": "yes"}'])


# ---------- through the gateway ----------


class FakeResponse:
    def __init__(self, text):
        self._d = {
            "id": "x",
            "object": "chat.completion",
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    def model_dump(self):
        return dict(self._d)


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def acompletion(model, messages, **kwargs):
        calls.append(messages[-1]["content"])
        return FakeResponse(f"answer #{len(calls)}")

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    return calls


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


def _config(router_env, semantic: str = "{ enabled: true }", policies: str = "version: 1\n"):
    router_env.write_text(
        textwrap.dedent(
            """
            aliases:
              smart-fast:
                - { provider: anthropic, model: claude-haiku-4-5, timeout_s: 5 }
                - { provider: ollama, model: llama3.1:8b, timeout_s: 5 }
            prices:
              "anthropic/claude-haiku-4-5": { input_usd_per_1m: 1.0, output_usd_per_1m: 5.0 }
            policies:
              cache_ttl_seconds: 0
            """
        )
        + f"semantic_cache: {semantic}\n"
    )
    Path(settings.ROUTER_POLICIES_FILE).write_text(textwrap.dedent(policies))
    config_loader.reload_all()


def _key(client, team="product"):
    r = client.post("/admin/keys", json={"label": f"{team}-k", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _ask(client, h, text, system=None, **extra):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
    body = {"model": "smart-fast", "messages": msgs, "temperature": 0, **extra}
    return client.post("/v1/chat/completions", json=body, headers=h)


def test_semantic_hit_serves_cached_answer_for_free(client, provider, router_env):
    _config(router_env)
    h = _key(client)
    first = _ask(client, h, "How do I export my invoices as CSV?")
    assert first.headers["x-router-cache"] == "miss" and first.headers["x-router-semantic-match"] == "empty"
    second = _ask(client, h, "how do I export my invoices as csv")
    assert second.headers["x-router-cache"] == "semantic-hit"
    assert float(second.headers["x-router-semantic-similarity"]) >= 0.88
    assert second.json()["choices"][0]["message"]["content"] == "answer #1" and len(provider) == 1
    recent = client.get("/admin/recent", headers=ADMIN).json()
    assert recent[0]["cached"] == 1 and recent[0]["cost_usd"] == 0 and recent[0]["saved_usd"] == pytest.approx(0.00035)
    assert client.get("/admin/semantic-cache", headers=ADMIN).json()["entries"] == 1
    assert 'router_semantic_cache_total{alias="smart-fast",result="hit"} 1' in metrics.render()


def test_number_guard_and_threshold_misses_call_the_provider(client, provider, router_env):
    _config(router_env)
    h = _key(client)
    _ask(client, h, "Is the 2025 model compatible with the charger?")
    r = _ask(client, h, "Is the 2023 model compatible with the charger?")
    assert r.headers["x-router-cache"] == "miss" and r.headers["x-router-semantic-match"] == "guard-numbers-differ"
    r = _ask(client, h, "What is the capital of Austria?")
    assert r.headers["x-router-semantic-match"] == "below_threshold" and len(provider) == 3


def test_cache_never_crosses_teams_policies_or_context(client, provider, router_env):
    q = "How do I export my invoices as CSV?"
    _config(router_env)
    product = _key(client, "product")
    _ask(client, product, q)
    assert _ask(client, _key(client, "support"), q).headers["x-router-cache"] == "miss"  # other team
    assert _ask(client, product, q, system="Answer in French.").headers["x-router-cache"] == "miss"  # other context
    assert _ask(client, product, q, max_tokens=50).headers["x-router-cache"] == "miss"  # other parameters
    assert _ask(client, product, q).headers["x-router-cache"] == "semantic-hit"
    _config(router_env, policies="teams: { product: { allowed_providers: [ollama] } }\n")
    r = _ask(client, product, q)
    assert r.headers["x-router-cache"] == "miss" and r.headers["x-router-used-provider"] == "ollama"  # other policy


def test_not_used_when_disabled_creative_or_streaming(client, provider, router_env):
    _config(router_env, "{ enabled: false }")
    h = _key(client)
    _ask(client, h, "same question")
    r = _ask(client, h, "same question")
    assert r.headers["x-router-cache"] == "miss" and "x-router-semantic-match" not in r.headers
    _config(router_env)
    _ask(client, h, "same question", temperature=0.7)
    assert "x-router-semantic-match" not in _ask(client, h, "same question", temperature=0.7).headers
    assert len(provider) == 4


def test_provider_embedder_and_failure_falls_through(client, provider, router_env, monkeypatch):
    seen = []

    async def aembedding(model, input, **kwargs):
        seen.append(model)
        if input[0].startswith("boom"):
            raise RuntimeError("embedding service down")
        return {"data": [{"embedding": [1.0, 0.0, 0.0] if "invoice" in input[0] else [0.0, 1.0, 0.0]}]}

    monkeypatch.setattr(litellm, "aembedding", aembedding)
    _config(router_env, "{ enabled: true, embedder: provider, embedding_model: openai/text-embedding-3-small }")
    h = _key(client)
    _ask(client, h, "invoice export please")
    assert _ask(client, h, "please export my invoice").headers["x-router-cache"] == "semantic-hit"
    assert seen == ["openai/text-embedding-3-small"] * 2
    r = _ask(client, h, "boom question")
    assert r.status_code == 200 and r.headers["x-router-cache"] == "miss"
    assert 'result="embed_error"} 1' in metrics.render()
    with pytest.raises(ValueError, match="embedding_model"):
        RouterConfig.model_validate(
            {"aliases": {"a": [{"provider": "ollama", "model": "m"}]}, "semantic_cache": {"embedder": "provider"}}
        )


def test_per_team_opt_in_and_threshold(client, provider, router_env):
    _config(router_env, "{ enabled: true, teams: [support, data], team_thresholds: { data: 0.99 } }")
    q, q2 = "How do I export my invoices as CSV?", "Please, how do I export my invoices as CSV"
    for team in ("product", "support", "data"):
        h = _key(client, team)
        _ask(client, h, q)
        r = _ask(client, h, q2)
        expected = {"product": None, "support": "hit", "data": "below_threshold"}[team]
        assert r.headers.get("x-router-semantic-match") == expected, team
    with pytest.raises(ValueError, match="team_thresholds"):
        RouterConfig.model_validate(
            {"aliases": {"a": [{"provider": "ollama", "model": "m"}]}, "semantic_cache": {"team_thresholds": {"x": 2}}}
        )

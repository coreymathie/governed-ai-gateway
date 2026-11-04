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
from scripts import build_semcache_heldout, build_semcache_pairs, calibrate_semcache

ROOT = Path(__file__).resolve().parent.parent
PAIRS = ROOT / "evals" / "semcache_pairs.jsonl"
HELDOUT = ROOT / "evals" / "semcache_heldout.jsonl"
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
    # number words count as numbers, and match their digits
    assert semcache.guard("Transfer five hundred dollars", "Transfer 500 dollars") is None
    assert semcache.guard("Transfer five hundred dollars", "Transfer 700 dollars") == "numbers differ"
    assert semcache.guard("Can I add twenty-five payees?", "Can I add 25 payees?") is None
    assert semcache.numbers("Which one has two-step login?") == ()  # a pronoun and a compound, not amounts
    # the old guard set, kept to reproduce the figures from before the opposite-action and entity guards
    assert semcache.guard("Send five dollars", "Send 5 dollars", checks=semcache.GUARDS_V1) == "numbers differ"
    assert semcache.guard("turn on alerts", "turn off alerts", checks=semcache.GUARDS_V1) is None


@pytest.mark.parametrize(
    ("a", "b", "why"),
    [
        ("Can I enable push alerts?", "Can I disable push alerts?", "opposite action (on/off)"),
        ("How do I turn the alerts off?", "How do I turn on the alerts?", "opposite action (on/off)"),
        ("Please freeze my card", "Please unfreeze my card", "opposite action (lock/unlock)"),
        ("Link my brokerage account", "Unlink my brokerage account", "opposite action (add/remove)"),
        ("When do you open on holidays?", "When do you close on holidays?", "opposite action (open/close)"),
        ("I want to start a payroll deduction", "I want to stop a payroll deduction", "opposite action (start/stop)"),
        ("Can I raise my card limit?", "Can I lower my card limit?", "opposite action (increase/decrease)"),
        ("Why was my claim approved?", "Why was my claim denied?", "opposite action (approve/deny)"),
        ("Can I pay before the due date?", "Can I pay after the due date?", "opposite action (before/after)"),
        ("Is there a fee to receive money?", "Is there a fee to send money?", "opposite action (send/receive)"),
        ("Report an unauthorized payment", "Report an authorized payment", "opposite action (authoriz/unauthoriz)"),
        ("Do I earn dividends on checking?", "Do I earn dividends on savings?", "different account: checking/savings"),
        ("Is my debit card contactless?", "Is my credit card contactless?", "different card: debit/credit"),
        ("What's my weekly ATM limit?", "What's my daily ATM limit?", "different period: weekly/daily"),
        ("Open on Monday?", "Open on Friday?", "different weekday: monday/friday"),
        ("Export my history as CSV", "Export my history as PDF", "different file format: csv/pdf"),
        ("Is the Riverside branch busy?", "Is the Hillcrest branch busy?", "different name: riverside/hillcrest"),
    ],
)
def test_guard_refuses_opposite_actions_and_swapped_entities(a, b, why):
    assert semcache.guard(a, b) == why
    assert semcache.guard(b, a) is not None  # either order


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("How do I enable alerts?", "How do I turn on alerts?"),  # synonyms share a pole
        ("Freeze my card please", "lock my card please"),
        ("Lock my card", "Lock my debit card"),  # one side does not name the kind at all
        ("Does the credit union offer a debit card?", "Do you offer a debit card at the credit union?"),  # not "credit"
        ("I cancelled the transfer", "cancel the transfer"),  # inflections meet
        ("Is the Riverside branch open late?", "is the riverside branch open late"),  # case is not a name change
    ],
)
def test_guard_lets_same_meaning_through(a, b):
    assert semcache.guard(a, b) is None


# ---------- cache mechanics ----------


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_lookup_threshold_guard_partition_ttl_and_lru():
    e, clock = semcache.HashingEmbedder(), Clock()
    c = semcache.SemanticCache(threshold=0.85, ttl_seconds=60, max_entries=2, clock=clock)
    q = "How do I download my statements as PDF?"
    c.put("team-a", q, e.embed(q), "answer-1")
    hit = c.lookup(
        "team-a", "how do I download my statements as pdf", e.embed("how do I download my statements as pdf")
    )
    assert hit.hit and hit.entry.value == "answer-1" and hit.reason == "hit"
    assert c.lookup("team-b", q, e.embed(q)).reason == "empty"  # partitions never mix
    far = c.lookup("team-a", "Is there parking at the branch?", e.embed("Is there parking at the branch?"))
    assert not far.hit and far.reason == "below_threshold"
    n, m = (
        "How many days do I have to report a 60 day old charge?",
        "How many days do I have to report a 90 day old charge?",
    )
    c.put("team-a", n, e.embed(n), "60-day answer")
    g = c.lookup("team-a", m, e.embed(m))
    assert not g.hit and g.reason == "guard: numbers differ" and g.similarity > 0.85
    c.put("team-a", "third entry", e.embed("third entry"), "x")  # evicts the least recently used
    assert c.size() == 2
    clock.t = 61
    assert c.lookup("team-a", n, e.embed(n)).reason == "empty"  # expired


def test_guards_apply_at_any_threshold_and_to_every_candidate():
    e = semcache.HashingEmbedder()
    on = "Hi, I want to turn on card alerts for my account in the mobile app. Where is that setting?"
    off = on.replace("turn on", "turn off")
    for t in (0.01, 0.5, 0.89, 0.94):  # however loose the threshold, the opposite never serves the cached answer
        c = semcache.SemanticCache(threshold=t)
        c.put("p", on, e.embed(on), "on-answer")
        found = c.lookup("p", off, e.embed(off))
        assert not found.hit and found.reason == "guard: opposite action (on/off)", t
    # a refused best match does not hide a second entry that does mean the same thing
    c = semcache.SemanticCache(threshold=0.89)
    para = "hi, i want to switch off card alerts for my account in the mobile app, where is that setting"
    c.put("p", on, e.embed(on), "on-answer")
    c.put("p", para, e.embed(para), "off-answer")
    found = c.lookup("p", off, e.embed(off))
    assert found.hit and found.entry.value == "off-answer"
    assert semcache.cosine(e.embed(off), e.embed(on)) > found.similarity  # the closer entry was the refused one


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
    """The figures quoted in the README: bundled fictional credit-union pairs, hashing-v1 embedder, target 1%."""
    assert build_semcache_pairs.main(["--check"]) == 0
    pairs = semcache.load_pairs(PAIRS.read_text().splitlines())
    assert len(pairs) == 1324 and sum(p.same for p in pairs) == 680
    assert sum(1 for p in pairs if not p.id.startswith("g")) == 160  # the hand-written pairs, unchanged
    rep = calibrate_semcache.run(str(PAIRS), 0.01, 0.50, 0.99, 0.01)
    assert rep["pairs"] == {"total": 1324, "calibration": 678, "holdout": 646}
    assert rep["chosen"]["threshold"] == 0.89  # picked on the development calibration half only
    assert (rep["chosen"]["hits"], rep["chosen"]["false_hits"], rep["chosen"]["hit_rate"]) == (156, 1, 0.4318)
    h, g = rep["holdout_at_chosen"], rep["holdout_without_guards_at_chosen"]
    assert (h["true_hits"], h["hits"], h["hit_rate"], h["false_hit_rate"]) == (129, 130, 0.4019, 0.0077)
    assert h["false_hit_rate_upper95"] == 0.0423
    assert (g["false_hits"], g["hits"], g["false_hit_rate"]) == (32, 162, 0.1975)
    # the held-out set: written after the development pairs had been read, never used for a choice
    ho = rep["heldout"]
    assert (ho["pairs"], ho["positives"]) == (2800, 1400)
    a, n = ho["at_chosen"], ho["without_guards_at_chosen"]
    assert (a["true_hits"], a["hits"], a["hit_rate"], a["false_hit_rate"]) == (503, 531, 0.3593, 0.0527)
    assert a["false_hit_rate_upper95"] == 0.0752  # above the 1% target: the cache stays off by default
    assert (n["false_hits"], n["hits"], n["true_hits"]) == (170, 675, 505)  # the guards cost 2 correct hits
    assert calibrate_semcache.run(str(PAIRS), 0.01, 0.5, 0.99, 0.01, conservative=True)["chosen"] is None


def test_before_fix_figures_reproduce():
    """The guards before the opposite-action and entity checks (numbers and negation only): the 'before' column."""
    rep = calibrate_semcache.run(str(PAIRS), 0.01, 0.50, 0.99, 0.01, guards="v1")
    assert rep["chosen"]["threshold"] == 0.97 and rep["guards"] == ["digits", "negation"]
    h, a = rep["holdout_at_chosen"], rep["heldout"]["at_chosen"]
    assert (h["true_hits"], h["hits"], h["hit_rate"], h["false_hit_rate_upper95"]) == (78, 80, 0.243, 0.0866)
    assert (a["true_hits"], a["hits"], a["hit_rate"], a["false_hit_rate"]) == (58, 62, 0.0414, 0.0645)
    assert a["false_hit_rate_upper95"] == 0.1545
    old = semcache.calibrate(semcache.split(semcache.load_pairs(PAIRS.read_text().splitlines()))[1],
                             semcache.HashingEmbedder(), [0.90], guards=semcache.GUARDS_V1)[0]  # fmt: skip
    assert (old["false_hits"], old["hits"], old["false_hit_rate"]) == (19, 140, 0.1357)  # the default before 0.97


def test_heldout_set_is_current_separate_and_balanced():
    assert build_semcache_heldout.main(["--check"]) == 0
    held = semcache.load_pairs(HELDOUT.read_text().splitlines())
    dev = semcache.load_pairs(PAIRS.read_text().splitlines())
    assert len(held) == 2800 and sum(p.same for p in held) == 1400 and all(p.id.startswith("h") for p in held)
    texts = {t.casefold() for p in dev for t in (p.a, p.b)}
    assert not any(t.casefold() in texts for p in held for t in (p.a, p.b))  # no question shared with development
    assert build_semcache_heldout.SEED != build_semcache_pairs.SEED


def test_calibration_script_outputs(tmp_path, capsys):
    assert calibrate_semcache.main(["--out", str(tmp_path / "cal")]) == 0
    md = (tmp_path / "cal.md").read_text()
    assert "0.89 **(chosen)**" in md and "Holdout at 0.89" in md and "Held-out set at 0.89" in md
    assert "not a prediction for production" in md
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


def _key(client, team="digital-banking"):
    r = client.post("/admin/keys", json={"label": f"{team}-k", "team": team}, headers=ADMIN)
    return {"Authorization": f"Bearer {r.json()['key']}"}


def _ask(client, h, text, system=None, **extra):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": text}]
    body = {"model": "smart-fast", "messages": msgs, "temperature": 0, **extra}
    return client.post("/v1/chat/completions", json=body, headers=h)


def test_semantic_hit_serves_cached_answer_for_free(client, provider, router_env):
    _config(router_env)
    h = _key(client)
    first = _ask(client, h, "How do I download my statements as PDF?")
    assert first.headers["x-router-cache"] == "miss" and first.headers["x-router-semantic-match"] == "empty"
    second = _ask(client, h, "how do I download my statements as pdf")
    assert second.headers["x-router-cache"] == "semantic-hit"
    assert float(second.headers["x-router-semantic-similarity"]) >= 0.90
    assert second.json()["choices"][0]["message"]["content"] == "answer #1" and len(provider) == 1
    recent = client.get("/admin/recent", headers=ADMIN).json()
    assert recent[0]["cached"] == 1 and recent[0]["cost_usd"] == 0 and recent[0]["saved_usd"] == pytest.approx(0.00035)
    assert client.get("/admin/semantic-cache", headers=ADMIN).json()["entries"] == 1
    assert 'router_semantic_cache_total{alias="smart-fast",result="hit"} 1' in metrics.render()


def test_number_guard_and_threshold_misses_call_the_provider(client, provider, router_env):
    _config(router_env, semantic="{ enabled: true, threshold: 0.90 }")  # low enough for the guard to decide
    h = _key(client)
    _ask(client, h, "How many days do I have to report a 60 day old charge?")
    r = _ask(client, h, "How many days do I have to report a 90 day old charge?")
    assert r.headers["x-router-cache"] == "miss" and r.headers["x-router-semantic-match"] == "guard-numbers-differ"
    r = _ask(client, h, "Is there parking at the branch?")
    assert r.headers["x-router-semantic-match"] == "below_threshold" and len(provider) == 3
    on = "Hi, I want to turn on card alerts for my account in the mobile app. Where is that setting?"
    _ask(client, h, on)
    r = _ask(client, h, on.replace("turn on", "turn off"))
    assert r.headers["x-router-semantic-match"] == "guard-opposite-action-on/off" and len(provider) == 5


def test_guard_switches_in_config(client, provider, router_env):
    _config(router_env, semantic="{ enabled: true, threshold: 0.80, guards: { opposites: false } }")
    h = _key(client)
    on = "Hi, I want to turn on card alerts for my account in the mobile app. Where is that setting?"
    _ask(client, h, on)
    r = _ask(client, h, on.replace("turn on", "turn off"))
    assert r.headers["x-router-cache"] == "semantic-hit"  # switched off, the opposite is served: why it is on
    cfg = RouterConfig.model_validate({"aliases": {"a": [{"provider": "ollama", "model": "m"}]}})
    assert all(cfg.semantic_cache.guards.model_dump().values())  # every guard is on unless switched off
    assert set(cfg.semantic_cache.guards.model_dump()) == {"numbers", "negation", "opposites", "entities"}


def test_cache_never_crosses_teams_policies_or_context(client, provider, router_env):
    q = "How do I download my statements as PDF?"
    _config(router_env)
    banking = _key(client, "digital-banking")
    _ask(client, banking, q)
    assert _ask(client, _key(client, "member-services"), q).headers["x-router-cache"] == "miss"  # other team
    assert _ask(client, banking, q, system="Answer in French.").headers["x-router-cache"] == "miss"  # other context
    assert _ask(client, banking, q, max_tokens=50).headers["x-router-cache"] == "miss"  # other parameters
    assert _ask(client, banking, q).headers["x-router-cache"] == "semantic-hit"
    _config(router_env, policies="teams: { digital-banking: { allowed_providers: [ollama] } }\n")
    r = _ask(client, banking, q)
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
        return {"data": [{"embedding": [1.0, 0.0, 0.0] if "statement" in input[0] else [0.0, 1.0, 0.0]}]}

    monkeypatch.setattr(litellm, "aembedding", aembedding)
    _config(router_env, "{ enabled: true, embedder: provider, embedding_model: openai/text-embedding-3-small }")
    h = _key(client)
    _ask(client, h, "statement download please")
    assert _ask(client, h, "please download my statement").headers["x-router-cache"] == "semantic-hit"
    assert seen == ["openai/text-embedding-3-small"] * 2
    r = _ask(client, h, "boom question")
    assert r.status_code == 200 and r.headers["x-router-cache"] == "miss"
    assert 'result="embed_error"} 1' in metrics.render()
    with pytest.raises(ValueError, match="embedding_model"):
        RouterConfig.model_validate(
            {"aliases": {"a": [{"provider": "ollama", "model": "m"}]}, "semantic_cache": {"embedder": "provider"}}
        )


def test_per_team_opt_in_and_threshold(client, provider, router_env):
    _config(
        router_env,
        "{ enabled: true, threshold: 0.90, teams: [member-services, risk-analytics],"
        " team_thresholds: { risk-analytics: 0.99 } }",
    )
    q, q2 = "How do I download my statements as PDF?", "Please, how do I download my statements as PDF"
    for team in ("digital-banking", "member-services", "risk-analytics"):
        h = _key(client, team)
        _ask(client, h, q)
        r = _ask(client, h, q2)
        expected = {"digital-banking": None, "member-services": "hit", "risk-analytics": "below_threshold"}[team]
        assert r.headers.get("x-router-semantic-match") == expected, team
    with pytest.raises(ValueError, match="team_thresholds"):
        RouterConfig.model_validate(
            {"aliases": {"a": [{"provider": "ollama", "model": "m"}]}, "semantic_cache": {"team_thresholds": {"x": 2}}}
        )

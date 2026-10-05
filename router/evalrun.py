# Corey Mathie, 2026
"""
Running evaluations from the command line (scripts/eval_routes.py, scripts/eval_gate.py).

Loads a routes file, picks the provider (simulated by default; real providers
only when every deployment in the selected routes has its credentials), runs
router/evals.py over the requested aliases and returns a report dict.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path

import yaml

from . import costs, evals
from .models import RouterConfig, RouteTarget

KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}


def load_routes(path: str | Path) -> RouterConfig:
    return RouterConfig.model_validate(yaml.safe_load(Path(path).read_text()))


def select(cfg: RouterConfig, aliases: Sequence[str] | None) -> dict[str, list[RouteTarget]]:
    names = list(aliases) if aliases else sorted(cfg.aliases)
    unknown = [a for a in names if a not in cfg.aliases]
    if unknown:
        raise evals.EvalConfigError(f"aliases not in the routes file: {unknown}")
    return {a: cfg.aliases[a] for a in names}


def missing_credentials(routes: dict[str, list[RouteTarget]]) -> list[str]:
    """Deployments a real run can't call: hosted providers need their API key; Ollama needs OLLAMA_HOST."""
    out = []
    for targets in routes.values():
        for t in targets:
            env = KEY_ENV.get(t.provider, "OLLAMA_HOST")
            if not os.environ.get(env):
                out.append(f"{t.provider}/{t.model} (needs {env})")
    return sorted(set(out))


def real_provider() -> evals.Provider:
    import litellm

    async def call(target: RouteTarget, case: evals.Case) -> evals.Completion:
        kwargs = {"api_base": os.environ["OLLAMA_HOST"]} if target.provider == "ollama" else {}
        if target.provider in KEY_ENV:
            kwargs["api_key"] = os.environ[KEY_ENV[target.provider]]
        started = time.perf_counter()
        resp = await litellm.acompletion(
            model=f"{target.provider}/{target.model}",
            messages=case.messages,
            temperature=0,
            max_tokens=case.max_tokens,
            timeout=target.timeout_s,
            **kwargs,
        )
        data = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return evals.Completion(
            text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
            time.perf_counter() - started,
        )  # fmt: skip

    return call


def load_judge(spec: str | None) -> evals.Judge | None:
    """`package.module:function` -> callable(case, output) -> float in [0, 1]."""
    if not spec:
        return None
    module, _, attr = spec.partition(":")
    if not attr:
        raise evals.EvalConfigError("judge must be given as module:function")
    return getattr(importlib.import_module(module), attr)


def run(
    routes_file: str | Path,
    cases_file: str | Path,
    aliases: Sequence[str] | None = None,
    provider: str = "sim",
    profiles_file: str | Path | None = "evals/sim_profiles.json",
    seed: str = "eval-v1",
    judge: str | None = None,
) -> dict:
    cfg = load_routes(routes_file)
    routes = select(cfg, aliases)
    cases = evals.load_cases(Path(cases_file).read_text().splitlines())
    before = costs.price_overrides()
    try:
        costs.set_price_overrides({k: (p.input_usd_per_1m, p.output_usd_per_1m) for k, p in cfg.prices.items()})
        if provider == "sim":
            raw = json.loads(Path(profiles_file).read_text()) if profiles_file else {}
            profiles = evals.load_profiles(raw.get("profiles", raw))
            evals.apply_sim_prices(profiles)  # simulated runs price with the profiles, not list prices
            call = evals.simulated_provider(profiles, seed)
            label = "simulated"
        elif provider == "real":
            missing = missing_credentials(routes)
            if missing:
                raise evals.EvalConfigError("real run refused, missing credentials for: " + ", ".join(missing))
            call = real_provider()
            label = "real"
        else:
            raise evals.EvalConfigError(f"unknown provider {provider!r} (sim or real)")
        judge_fn = load_judge(judge)

        async def go():
            return {name: await evals.run_route(name, t, cases, call, judge_fn) for name, t in routes.items()}

        results = asyncio.run(go())
    finally:
        costs.set_price_overrides(before)
    rep = evals.report(routes, results, label, str(cases_file))
    rep["seed"] = seed if provider == "sim" else None
    rep["routes_file"] = str(routes_file)
    return rep

# Corey Mathie, 2026
"""
Gateway glue for policy-as-code (router/policy.py) and the optional OPA check
(router/opa.py): evaluate before routing, fail closed on any policy error,
audit every decision, and describe it in response headers.

Headers on every response that reached policy evaluation:
  x-router-policy            allow | deny
  x-router-policy-rules      layers that applied, e.g. defaults,teams.regulated
  x-router-policy-source     local | local+opa
  x-router-policy-removed    deployments the policy took out of the route
  x-router-policy-max-tokens effective max_tokens when the policy set or lowered it
"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException

from . import audit, metrics, opa
from .config_loader import current_policies, settings
from .models import ApiKey, ChatCompletionRequest, RouteTarget
from .policy import Decision, RequestFacts, apply_opa, evaluate, opa_input
from .privacy import key_fp

log = logging.getLogger("router")


def _ascii(v: str) -> str:
    return v.encode("ascii", "replace").decode()[:512]


def headers(d: Decision) -> dict[str, str]:
    h = {
        "x-router-policy": "allow" if d.allow else "deny",
        "x-router-policy-rules": _ascii(",".join(d.rules)),
        "x-router-policy-source": d.source,
    }
    if d.removed:
        h["x-router-policy-removed"] = _ascii(",".join(r["deployment"] for r in d.removed))
    if d.max_tokens_clamped and d.allow:
        h["x-router-policy-max-tokens"] = str(d.max_tokens)
    return h


def request_bytes(body: ChatCompletionRequest) -> int:
    return len(json.dumps([m.model_dump() for m in body.messages], ensure_ascii=False).encode())


async def evaluate_request(
    key: ApiKey, body: ChatCompletionRequest, targets: list[RouteTarget]
) -> tuple[Decision, bool]:
    """Local policy, then OPA if configured. Never raises: any failure is a deny. Returns (decision, errored)."""
    facts = RequestFacts(
        team=key.team,
        alias=body.model,
        targets=targets,
        max_tokens=body.max_tokens,
        request_bytes=request_bytes(body),
        messages=len(body.messages),
        key_label=key.label,
        stream=body.stream,
    )
    policies = current_policies()
    try:
        decision = evaluate(policies, facts)
        if decision.allow and (settings.OPA_URL or policies.opa.enabled):
            if not settings.OPA_URL:
                decision.source = "local+opa"
                decision.allow, decision.status, decision.targets = False, 503, []
                decision.reasons.append("policies.opa.enabled is true but OPA_URL is not set")
            else:
                result = await opa.query(
                    settings.OPA_URL, policies.opa.path, opa_input(decision, facts), policies.opa.timeout_s
                )
                decision = apply_opa(decision, result)
        return decision, False
    except Exception as e:  # noqa: BLE001 - any policy failure denies the request (fail closed)
        log.error("policy evaluation failed for team %s alias %s: %s", key.team, body.model, e)
        decision = Decision(allow=False, status=503, reasons=[f"policy evaluation failed ({type(e).__name__})"])
        decision.source = "local+opa" if settings.OPA_URL else "local"
        return decision, True


async def decide(key: ApiKey, body: ChatCompletionRequest, targets: list[RouteTarget]) -> Decision:
    """Evaluate, audit, and raise the HTTP error for a deny."""
    decision, errored = await evaluate_request(key, body, targets)
    action = "error" if errored else ("allow" if decision.allow else "deny")
    audit.record("policy", action, team=key.team, key_fp=key_fp(key), subject=body.model, detail=decision.as_dict())
    if errored:
        metrics.rejection("policy_error")
        raise HTTPException(503, "policy decision unavailable; request refused", headers=headers(decision))
    if not decision.allow:
        metrics.rejection(f"policy_{decision.status}")
        raise HTTPException(decision.status, "policy: " + "; ".join(decision.reasons), headers=headers(decision))
    return decision

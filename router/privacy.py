# Corey Mathie, 2026
"""
Gateway glue for content hooks (router/pii.py): run them on requests and
responses, audit what they did, fail closed when they break, and write the
opt-in, always-redacted content log.

Defaults: no hooks, and no prompt or completion text stored anywhere (usage
rows, logs, spans and the audit trail carry metadata only). A team opts in to
content logging with `privacy.teams.<team>.log_content: true`; what gets
stored is then redacted with every detector regardless of the hooks configured,
and truncated to `content_log_max_chars`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from fastapi import HTTPException

from . import audit, metrics
from .models import ApiKey, ChatCompletionRequest, RouterConfig
from .pii import KINDS, HookContext, HookError, redact, run_hooks
from .store import content_log_put

log = logging.getLogger("router")


def key_fp(key: ApiKey) -> str:
    return key.fingerprint


@dataclass
class ContentPolicy:
    """The hooks and logging that apply to one request (team + alias)."""

    pre: list[str]
    post: list[str]
    kinds: tuple[str, ...]
    log_content: bool
    max_chars: int
    findings: dict[str, dict[str, int]] = field(default_factory=dict)  # stage -> kind -> count

    @classmethod
    def for_request(cls, cfg: RouterConfig, team: str, alias: str, extra_pre: list[str] | None = None) -> ContentPolicy:
        p = cfg.privacy
        pre = p.hooks_for(team, alias, "pre_request")
        for name in extra_pre or []:  # hooks a policy requires (router/policy.py) are added, never removed
            if name not in pre:
                pre.append(name)
        return cls(
            pre=pre,
            post=p.hooks_for(team, alias, "post_response"),
            kinds=tuple(p.kinds),
            log_content=p.logs_content(team),
            max_chars=p.content_log_max_chars,
        )

    def fingerprint(self) -> str:
        """Part of every cache key, so a cached answer is only reused under the same content policy."""
        material = json.dumps([self.pre, self.post, list(self.kinds)])
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def headers(self) -> dict[str, str]:
        h = {}
        if self.pre or self.post:
            h["x-router-content-hooks"] = ",".join(dict.fromkeys(self.pre + self.post))
        total: dict[str, int] = {}
        for stage in self.findings.values():
            for k, v in stage.items():
                total[k] = total.get(k, 0) + v
        if total:
            h["x-router-redactions"] = ",".join(f"{k}={v}" for k, v in sorted(total.items()))
        return h


def _run(names: list[str], texts: list[str], stage: str, key: ApiKey, alias: str, policy: ContentPolicy) -> list[str]:
    ctx = HookContext(stage, key.team, alias, policy.kinds)
    try:
        run = run_hooks(names, texts, ctx)
    except HookError as e:
        log.error("content hook failed (%s, alias %s): %s", stage, alias, e)
        audit.record("privacy", "hook_error", team=key.team, key_fp=key_fp(key), subject=alias, detail={"stage": stage})
        metrics.rejection("privacy_hook_error")
        raise HTTPException(503, "content policy hook failed; nothing was sent or returned") from None
    for a in run.actions:
        audit.record(
            "privacy",
            a["action"],
            team=key.team,
            key_fp=key_fp(key),
            subject=alias,
            detail={"stage": stage, "hook": a["hook"], "findings": a["findings"]},
        )
    if run.findings():
        policy.findings[stage] = run.findings()
    if run.blocked:
        metrics.rejection(f"privacy_block_{'request' if stage == 'pre_request' else 'response'}")
        if stage == "pre_request":
            raise HTTPException(422, f"blocked by content policy ({run.blocked_by}): {run.reason}")
        raise HTTPException(422, f"response withheld by content policy ({run.blocked_by}): {run.reason}")
    return run.texts


def apply_pre(key: ApiKey, body: ChatCompletionRequest, policy: ContentPolicy) -> ChatCompletionRequest:
    if not policy.pre:
        return body
    texts = _run(policy.pre, [m.content for m in body.messages], "pre_request", key, body.model, policy)
    msgs = [m.model_copy(update={"content": t}) for m, t in zip(body.messages, texts, strict=True)]
    return body.model_copy(update={"messages": msgs})


def apply_post(key: ApiKey, alias: str, data: dict, policy: ContentPolicy) -> dict:
    """Run post_response hooks over each choice's message content (in place on a copy)."""
    if not policy.post:
        return data
    choices = data.get("choices") or []
    slots = [(i, (c.get("message") or {}).get("content")) for i, c in enumerate(choices)]
    slots = [(i, t) for i, t in slots if isinstance(t, str)]
    if not slots:
        return data
    texts = _run(policy.post, [t for _, t in slots], "post_response", key, alias, policy)
    data = dict(data)
    data["choices"] = [dict(c) for c in choices]
    for (i, _), t in zip(slots, texts, strict=True):
        data["choices"][i]["message"] = {**data["choices"][i]["message"], "content": t}
    return data


def response_text(data: dict) -> str:
    parts = []
    for c in data.get("choices") or []:
        t = (c.get("message") or {}).get("content")
        if isinstance(t, str):
            parts.append(t)
    return "\n".join(parts)


def log_content(key: ApiKey, body: ChatCompletionRequest, response: str, policy: ContentPolicy) -> None:
    """Store redacted prompt and completion for teams that opted in. Never raises into the request."""
    if not policy.log_content:
        return
    try:
        req_raw = "\n".join(f"{m.role}: {m.content}" for m in body.messages)
        req, req_counts = redact(req_raw, KINDS)
        resp, resp_counts = redact(response, KINDS)
        counts = {"request": req_counts, "response": resp_counts}
        content_log_put(
            key.team,
            key_fp(key),
            body.model,
            req[: policy.max_chars],
            resp[: policy.max_chars],
            json.dumps(counts, sort_keys=True),
        )
        audit.record("privacy", "content_logged", team=key.team, key_fp=key_fp(key), subject=body.model, detail=counts)
    except Exception:
        log.exception("content log write failed for team %s", key.team)

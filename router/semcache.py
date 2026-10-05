# Corey Mathie, 2026
"""
Semantic response cache: reuse an answer when a new request means the same
thing as a cached one, not only when it is byte-for-byte identical.

Pieces:
  HashingEmbedder   deterministic, offline default: word unigrams, word bigrams
                    and character trigrams, signed feature hashing into `dim`
                    buckets, L2-normalised. No model, no network. It captures
                    wording overlap, not meaning: paraphrases that share few
                    words score low.
  Embedder          anything with embed(text) -> list[float]; the gateway can
                    use a provider embedding model instead (router/routing.py).
  guards            a candidate only counts as a hit if both texts contain the
                    same numbers and the same negations ("not", "never", "no",
                    "n't"): "convert 5 km" must not reuse "convert 7 km".
  SemanticCache     partitions -> entries with TTL and LRU eviction. The
                    partition key is built by the caller from team, alias,
                    policy decision, content hooks, earlier conversation turns
                    and generation parameters, so a lookup can never return an
                    entry from another team or another policy.
  calibrate()       hit rate and false-hit rate at candidate thresholds on a
                    labelled pair set; pick_threshold() takes the lowest
                    threshold whose false-hit rate meets a target.

Pure Python (stdlib only); the browser demo loads this file unchanged.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NEGATION = re.compile(r"\b(?:not|no|never|none|nothing|without)\b|n't\b")


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
    return h % dim, (1.0 if (h >> 63) & 1 else -1.0)


@dataclass
class HashingEmbedder:
    dim: int = 1024
    unigram_weight: float = 1.0
    bigram_weight: float = 0.5
    trigram_weight: float = 0.25
    name: str = "hashing-v1"

    def embed(self, text: str) -> list[float]:
        words = _WORD.findall(text.casefold())
        vec = [0.0] * self.dim
        feats: list[tuple[str, float]] = [(f"w:{w}", self.unigram_weight) for w in words]
        feats += [(f"b:{a} {b}", self.bigram_weight) for a, b in itertools.pairwise(words)]
        for w in words:
            padded = f"#{w}#"
            feats += [(f"c:{padded[i : i + 3]}", self.trigram_weight) for i in range(len(padded) - 2)]
        for feat, weight in feats:
            i, sign = _bucket(feat, self.dim)
            vec[i] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vectors have different dimensions")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def numbers(text: str) -> tuple[str, ...]:
    return tuple(sorted(n.replace(",", "") for n in _NUMBER.findall(text)))


def negations(text: str) -> int:
    return len(_NEGATION.findall(text.casefold()))


def guard(a: str, b: str, check_numbers: bool = True, check_negation: bool = True) -> str | None:
    """Why two texts must not share an answer despite similar wording, or None."""
    if check_numbers and numbers(a) != numbers(b):
        return "numbers differ"
    if check_negation and (negations(a) > 0) != (negations(b) > 0):
        return "negation differs"
    return None


# ---------- the cache ----------


@dataclass
class Entry:
    text: str
    vector: list[float]
    value: Any
    created: float
    hits: int = 0


@dataclass
class Lookup:
    hit: bool
    similarity: float = 0.0  # best candidate's similarity, hit or not
    entry: Entry | None = None
    reason: str = "empty"  # hit | below_threshold | guard: <why> | empty


@dataclass
class SemanticCache:
    threshold: float
    ttl_seconds: float = 3600.0
    max_entries: int = 1000  # per partition; least recently used is evicted
    check_numbers: bool = True
    check_negation: bool = True
    clock: Callable[[], float] = time.monotonic
    _parts: dict[str, OrderedDict[int, Entry]] = field(default_factory=dict)
    _seq: int = 0

    def _live(self, partition: str) -> OrderedDict[int, Entry]:
        part = self._parts.get(partition)
        if part is None:
            return OrderedDict()
        now = self.clock()
        for k in [k for k, e in part.items() if now - e.created > self.ttl_seconds]:
            del part[k]
        return part

    def lookup(self, partition: str, text: str, vector: Sequence[float]) -> Lookup:
        part = self._live(partition)
        best: tuple[float, int, Entry] | None = None
        for k, e in part.items():
            s = cosine(vector, e.vector)
            if best is None or s > best[0]:
                best = (s, k, e)
        if best is None:
            return Lookup(False)
        sim, k, e = best
        if sim < self.threshold:
            return Lookup(False, sim, e, "below_threshold")
        why = guard(text, e.text, self.check_numbers, self.check_negation)
        if why:
            return Lookup(False, sim, e, f"guard: {why}")
        part.move_to_end(k)
        e.hits += 1
        return Lookup(True, sim, e, "hit")

    def put(self, partition: str, text: str, vector: Sequence[float], value: Any) -> None:
        part = self._parts.setdefault(partition, OrderedDict())
        self._seq += 1
        part[self._seq] = Entry(text, list(vector), value, self.clock())
        while len(part) > self.max_entries:
            part.popitem(last=False)

    def size(self) -> int:
        return sum(len(p) for p in self._parts.values())

    def partitions(self) -> int:
        return len(self._parts)

    def clear(self) -> None:
        self._parts.clear()


def partition_key(*parts: object) -> str:
    """Stable key from everything that must match exactly for two requests to share an answer."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:32]


# ---------- calibration ----------


@dataclass
class Pair:
    id: str
    a: str
    b: str
    same: bool  # True when one answer serves both


def load_pairs(lines: Iterable[str]) -> list[Pair]:
    import json

    out = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw = json.loads(line)
        if set(raw) != {"id", "a", "b", "same"} or not isinstance(raw["same"], bool):
            raise ValueError(f"line {n}: expected keys id, a, b, same (bool)")
        out.append(Pair(str(raw["id"]), raw["a"], raw["b"], raw["same"]))
    return out


def split(pairs: Sequence[Pair], holdout_share: float = 0.5, salt: str = "split-v1") -> tuple[list[Pair], list[Pair]]:
    """Deterministic calibration/holdout split by a hash of each pair id."""
    cal, hold = [], []
    for p in pairs:
        u = int.from_bytes(hashlib.sha256(f"{salt}|{p.id}".encode()).digest()[:8], "big") / 2**64
        (hold if u < holdout_share else cal).append(p)
    return cal, hold


def calibrate(
    pairs: Sequence[Pair],
    embedder: Embedder,
    thresholds: Sequence[float],
    use_guards: bool = True,
) -> list[dict]:
    """For each threshold: how many same-meaning pairs would hit (hit rate) and how many hits are wrong.

    false_hit_rate = wrong hits / all hits (what a caller experiences); fpr = wrong hits / different-meaning pairs.
    """
    scored = []
    for p in pairs:
        sim = cosine(embedder.embed(p.a), embedder.embed(p.b))
        blocked = guard(p.a, p.b) is not None if use_guards else False
        scored.append((sim, blocked, p.same))
    positives = sum(1 for _, _, same in scored if same)
    negatives = len(scored) - positives
    rows = []
    for t in thresholds:
        hits = [(same) for sim, blocked, same in scored if sim >= t and not blocked]
        true_hits = sum(1 for same in hits if same)
        false_hits = len(hits) - true_hits
        rows.append(
            {
                "threshold": round(t, 4),
                "hits": len(hits),
                "true_hits": true_hits,
                "false_hits": false_hits,
                "hit_rate": round(true_hits / positives, 4) if positives else 0.0,
                "false_hit_rate": round(false_hits / len(hits), 4) if hits else 0.0,
                "false_hit_rate_upper95": round(wilson_upper(false_hits, len(hits)), 4),
                "fpr": round(false_hits / negatives, 4) if negatives else 0.0,
            }
        )
    return rows


def wilson_upper(k: int, n: int, z: float = 1.96) -> float:
    """95% Wilson score upper bound on a proportion k/n (1.0 when n == 0)."""
    if n == 0:
        return 1.0
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / (1 + z * z / n))


def pick_threshold(
    rows: Sequence[Mapping], target_false_hit_rate: float, min_hits: int = 1, use_upper_bound: bool = False
) -> dict | None:
    """Lowest threshold such that it, and every higher threshold with hits, meets the false-hit target.

    False-hit rate isn't monotonic in the threshold on a finite set, so a lone dip below the target
    doesn't count: the pick is the bottom of the contiguous safe band at the top of the grid. With
    use_upper_bound, the 95% upper bound must meet the target instead of the point estimate, which a
    small pair set usually can't satisfy (0 wrong out of n hits still bounds the rate at about 3.8/n).
    """
    key = "false_hit_rate_upper95" if use_upper_bound else "false_hit_rate"
    best = None
    for r in sorted(rows, key=lambda r: r["threshold"], reverse=True):
        if r["hits"] == 0:
            continue
        if r[key] > target_false_hit_rate:
            break
        if r["hits"] >= min_hits:
            best = r
    return best


def threshold_grid(lo: float = 0.50, hi: float = 0.99, step: float = 0.01) -> list[float]:
    n = round((hi - lo) / step)
    return [round(lo + i * step, 4) for i in range(n + 1)]


def calibration_report(
    pairs: Sequence[Pair],
    target: float = 0.01,
    grid: Sequence[float] | None = None,
    conservative: bool = False,
    embedder: Embedder | None = None,
) -> dict:
    """Pick a threshold on the calibration half, then measure it on the held-out half (which played no part)."""
    emb = embedder or HashingEmbedder()
    cal, hold = split(pairs)
    cal_rows = calibrate(cal, emb, grid or threshold_grid())
    pick = pick_threshold(cal_rows, target, use_upper_bound=conservative)
    out = {
        "embedder": getattr(emb, "name", type(emb).__name__),
        "pairs": {"total": len(pairs), "calibration": len(cal), "holdout": len(hold)},
        "positives": {"calibration": sum(p.same for p in cal), "holdout": sum(p.same for p in hold)},
        "target_false_hit_rate": target,
        "rule": "95% upper bound meets target" if conservative else "point estimate meets target",
        "calibration_grid": cal_rows,
        "chosen": pick,
        "holdout_at_chosen": None,
        "holdout_without_guards_at_chosen": None,
    }
    if pick:
        t = [pick["threshold"]]
        out["holdout_at_chosen"] = calibrate(hold, emb, t)[0]
        out["holdout_without_guards_at_chosen"] = calibrate(hold, emb, t, use_guards=False)[0]
    return out

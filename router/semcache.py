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
  guards            a candidate only counts as a hit if nothing in the two texts
                    says they ask different things, however similar they are:
                    the same numbers (digits or number words: "convert 5 km"
                    must not reuse "convert seven km"), the same negations
                    ("not", "never", "no", "n't"), no action against its
                    opposite (turn on/off, add/remove, open/close, lock/unlock,
                    start/stop, increase/decrease, approve/deny, before/after,
                    more/less, send/receive, deposit/withdraw, and un-/dis-
                    forms such as "authorized"/"unauthorized"), no swapped
                    entity value of one kind (checking/savings, debit/credit,
                    daily/monthly, Saturday/Sunday, APY/APR, PDF/CSV, ...), and
                    no swapped capitalised name ("Riverside branch" vs
                    "Hillcrest branch"). Every candidate at or above the threshold is
                    checked, best first, at any threshold.
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


# ---------- guards ----------
#
# The lexicon below is written from general classes (antonym pairs of actions, and the kinds of entity a
# financial-services question names), not from the calibration pairs. Each class has poles; a pole lists the
# words and phrases that mean it. Two texts that both mention a class must mention the same poles in the same
# order. A text that does not mention a class at all says nothing about it, so "lock my card" and "lock my
# debit card" can still share an answer; the cost of a guard is a cache miss, never a wrong answer.

OPPOSITES: dict[str, dict[str, list[str]]] = {
    "on/off": {
        "on": ["turn on", "switch on", "enable", "activate", "reactivate", "opt in", "opt into", "resume", "unpause"],
        "off": ["turn off", "switch off", "shut off", "disable", "deactivate", "opt out", "pause", "suspend"],
    },
    "lock/unlock": {"lock": ["lock", "freeze", "block"], "unlock": ["unlock", "unfreeze", "unblock"]},
    "add/remove": {
        "add": ["add", "include", "attach", "link", "connect", "insert"],
        "remove": ["remove", "delete", "drop", "take off", "unlink", "disconnect", "exclude", "detach"],
    },
    "open/close": {"open": ["open", "reopen"], "close": ["close", "shut"]},
    "start/stop": {
        "start": ["start", "begin", "set up", "setup", "create", "initiate", "sign up", "enroll", "subscribe"],
        "stop": ["stop", "cancel", "terminate", "unsubscribe", "unenroll", "discontinue", "quit"],
    },
    "increase/decrease": {
        "increase": ["increase", "raise", "extend", "upgrade", "boost"],
        "decrease": ["decrease", "reduce", "lower", "shorten", "downgrade", "cut"],
    },
    "more/less": {
        "more": ["more", "higher", "larger", "bigger", "greater", "maximum", "max", "most"],
        "less": ["less", "fewer", "smaller", "minimum", "min", "least"],
    },
    "approve/deny": {
        "approve": ["approve", "accept", "allow", "permit", "grant", "authorize"],
        "deny": ["deny", "decline", "reject", "refuse", "disallow", "revoke"],
    },
    "before/after": {
        "before": ["before", "prior to", "earlier than", "ahead of"],
        "after": ["after", "later than", "following"],
    },
    "send/receive": {"send": ["send", "sent", "outgoing", "outbound"], "receive": ["receive", "incoming", "inbound"]},
    "deposit/withdraw": {
        "deposit": ["deposit", "put in", "pay in"],
        "withdraw": ["withdraw", "withdrew", "withdrawal", "take out", "cash out"],
    },
    "buy/sell": {"buy": ["buy", "purchase"], "sell": ["sell"]},
    "borrow/lend": {"borrow": ["borrow"], "lend": ["lend"]},
    "sign in/out": {
        "in": ["log in", "sign in", "login", "signin"],
        "out": ["log out", "sign out", "logout", "signout"],
    },
}

_RELATIVE_DATES = ["today", "tonight", "tomorrow", "yesterday", "this week", "next week", "last week", "this month",
                   "next month", "last month", "this year", "next year", "last year"]  # fmt: skip
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "weekday", "weekend"]
_MONTHS = ["january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
           "december"]  # fmt: skip

ENTITIES: dict[str, dict[str, list[str]]] = {
    "card": {"debit": ["debit"], "credit": ["credit"], "prepaid": ["prepaid", "gift card"]},
    "region": {"domestic": ["domestic"], "international": ["international", "foreign", "overseas", "abroad"]},
    "condition": {"new": ["new"], "used": ["used", "pre-owned"], "refinance": ["refinance", "refi"]},
    "owner": {"personal": ["personal"], "business": ["business", "commercial", "corporate"]},
    "account": {
        "checking": ["checking"],
        "savings": ["savings", "saving"],
        "money market": ["money market"],
        "certificate": ["certificate", "cd", "share certificate", "term deposit"],
        "ira": ["ira", "roth", "retirement"],
        "hsa": ["hsa", "health savings"],
        "club": ["club"],
        "trust": ["trust"],
        "youth": ["youth", "kids", "teen", "student checking"],
    },
    "loan": {
        "auto": ["auto", "car", "vehicle"],
        "boat": ["boat"],
        "rv": ["rv", "camper"],
        "motorcycle": ["motorcycle"],
        "mortgage": ["mortgage", "home loan"],
        "home equity": ["home equity", "heloc"],
        "student": ["student loan"],
        "line of credit": ["line of credit"],
    },
    "payment rail": {
        "wire": ["wire"],
        "ach": ["ach"],
        "person-to-person": ["zelle", "venmo", "paypal"],
        "external transfer": ["external transfer"],
        "internal transfer": ["internal transfer"],
        "bill pay": ["bill pay"],
        "cashier's check": ["cashier's check", "official check"],
        "money order": ["money order"],
        "mobile deposit": ["mobile deposit", "mobile check deposit"],
        "atm": ["atm"],
    },
    "setting": {
        "alerts": ["alert", "notification"],
        "paperless": ["paperless", "e-statement", "estatement", "electronic statement"],
        "two-step": ["two-step", "2fa", "mfa", "two-factor", "multi-factor", "one-time passcode"],
        "biometric": ["biometric", "fingerprint", "face id", "touch id"],
        "overdraft": ["overdraft"],
        "travel notice": ["travel notice", "travel notification"],
    },
    "role": {
        "joint owner": ["joint owner", "co-owner", "joint account holder"],
        "signer": ["signer"],
        "authorized user": ["authorized user"],
        "beneficiary": ["beneficiary", "payable-on-death", "pod"],
        "power of attorney": ["power of attorney", "poa"],
        "trustee": ["trustee"],
        "co-signer": ["co-signer", "cosigner", "co-borrower"],
        "custodian": ["custodian", "guardian"],
    },
    "credential": {
        "password": ["password", "passcode"],
        "username": ["username", "user id", "user name"],
        "pin": ["pin"],
        "security question": ["security question"],
    },
    "term": {
        "apy": ["apy", "annual percentage yield"],
        "apr": ["apr", "annual percentage rate"],
        "dividend": ["dividend rate", "dividend"],
        "interest rate": ["interest rate"],
        "minimum": ["minimum deposit", "minimum balance", "opening deposit"],
        "penalty": ["penalty"],
        "fee": ["fee", "cost", "charge", "price"],
        "limit": ["limit"],
    },
    "period": {
        "daily": ["daily", "per day", "each day"],
        "weekly": ["weekly", "per week"],
        "monthly": ["monthly", "per month", "each month"],
        "quarterly": ["quarterly"],
        "yearly": ["yearly", "annual", "annually", "per year"],
    },
    "relative date": {d: [d] for d in _RELATIVE_DATES},
    "weekday": {d: [d] for d in _WEEKDAYS},
    "month": {m: [m] for m in _MONTHS},  # "may" is left out: it is far more often the verb
    "file format": {
        "pdf": ["pdf"],
        "csv": ["csv"],
        "excel": ["excel", "xlsx", "xls"],
        "quicken": ["quicken", "qfx"],
        "ofx": ["ofx"],
        "quickbooks": ["quickbooks", "qbo"],
    },
}

# Phrases that contain a pole word without meaning it ("credit" in "credit union" is not a credit card).
_STOP_PHRASES = re.compile(r"\bcredit (?:union|score|report|bureau)s?\b")
# Particle verbs whose particle can come later: "turn it off", "switch the alerts back on", "take him off".
_PARTICLES = {"turn": {"on", "off"}, "switch": {"on", "off"}, "shut": {"off"}, "take": {"off", "out"}}
_PARTICLE_GAP = 3  # words allowed between the verb and its particle
_PREFIXES = ("un", "dis")  # "authorized"/"unauthorized", "continue"/"discontinue"
_MIN_BASE = 4  # a prefixed form only counts against a base word at least this long ("until" is not un-"til")
_NUMBER_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen".split()
    )
} | {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}


def _tokens(text: str) -> list[str]:
    """Casefolded words, hyphens split, possessive 's dropped, a plural -s dropped ("alerts" -> "alert")."""
    text = _STOP_PHRASES.sub(" ", text.casefold().replace("\u2019", "'"))
    out = []
    for w in _WORD.findall(text.replace("-", " ")):
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.append(w)
    return out


def _stem(w: str) -> str:
    """A light suffix strip so "enabled", "enabling" and "enable" meet; it only needs to be consistent."""
    if len(w) > 4 and w.endswith("ied"):
        return w[:-3] + "y"  # denied -> deny
    if len(w) > 3 and w.endswith("ie"):
        return w[:-2] + "y"  # denies (plural -s already dropped) -> deny
    for suffix in ("ing", "ed"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
                w = w[:-1]  # stopped -> stop, shutting -> shut ("cancelled" -> "cancell" -> below)
            break
    if len(w) > 4 and w.endswith("ll"):
        w = w[:-1]
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def _compile(classes: dict[str, dict[str, list[str]]], stem: bool) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    """Per class: (phrase as words, pole), longest phrase first so "share certificate" beats "certificate"."""
    out = {}
    for cls, poles in classes.items():
        phrases = []
        for pole, words in poles.items():
            for phrase in words:
                toks = tuple(_tokens(phrase))
                phrases.append((tuple(_stem(t) for t in toks) if stem else toks, pole))
        out[cls] = sorted(phrases, key=lambda p: -len(p[0]))
    return out


_OPPOSITES = _compile(OPPOSITES, stem=True)
_PARTICLES_STEMMED = {_stem(verb): parts for verb, parts in _PARTICLES.items()}
_ENTITIES = _compile(ENTITIES, stem=False)


def _with_particles(words: list[str]) -> list[str]:
    """Move a split particle next to its verb: "turn the alerts off" reads as "turn off the alerts"."""
    out = list(words)
    for i, w in enumerate(words):
        parts = _PARTICLES_STEMMED.get(w)
        if not parts or (i + 1 < len(words) and words[i + 1] in parts):
            continue
        for j in range(i + 2, min(len(words), i + 2 + _PARTICLE_GAP)):
            if words[j] in parts:
                out[i], out[j] = f"{w} {words[j]}", ""
                break
    return [t for w in out for t in w.split(" ") if t]


def _poles(words: Sequence[str], compiled: dict[str, list[tuple[tuple[str, ...], str]]]) -> dict[str, tuple[str, ...]]:
    """For each class a text mentions, the poles it names in order (repeats collapsed)."""
    found: dict[str, list[str]] = {}
    for cls, phrases in compiled.items():
        seq: list[str] = []
        i = 0
        while i < len(words):
            for phrase, pole in phrases:
                if tuple(words[i : i + len(phrase)]) == phrase:
                    if not seq or seq[-1] != pole:
                        seq.append(pole)
                    i += len(phrase)
                    break
            else:
                i += 1
        if seq:
            found[cls] = seq
    return {cls: tuple(seq) for cls, seq in found.items()}


def _word_numbers(words: Sequence[str]) -> list[str]:
    """Spelled-out amounts as digits: "five hundred" -> "500", "twenty five" -> "25". A lone "one" is a pronoun."""
    out, total, current, run = [], 0, 0, []
    for w in [*words, ""]:
        if w in _NUMBER_WORDS or w in _SCALES or (w in ("a", "and") and run):
            run.append(w)
            if w in _NUMBER_WORDS:
                current += _NUMBER_WORDS[w]
            elif w in _SCALES:
                scale = _SCALES[w]
                current = max(current, 1) * scale
                if scale >= 1000:
                    total, current = total + current, 0
            continue
        meaningful = [r for r in run if r not in ("a", "and")]
        if meaningful and meaningful != ["one"]:
            out.append(str(total + current))
        total, current, run = 0, 0, []
        if w == "a":
            run.append(w)  # "a hundred"
    return out


def numbers(text: str) -> tuple[str, ...]:
    """Every amount in a text, digits or words, normalised: "$1,200" and "1200" match, "five" matches "5"."""
    digits = [n.replace(",", "") for n in _NUMBER.findall(text)]
    # Hyphenated words only count when every part is a number word ("twenty-five", not "two-step").
    words = []
    for chunk in re.findall(r"[a-z]+(?:-[a-z]+)*", text.casefold()):
        parts = chunk.split("-")
        if len(parts) == 1 or all(p in _NUMBER_WORDS or p in _SCALES for p in parts):
            words += parts
        else:
            words.append("|")  # a non-number word ends a spelled-out amount
    return tuple(sorted(digits + _word_numbers(words)))


def negations(text: str) -> int:
    return len(_NEGATION.findall(text.casefold()))


def opposite_action(a: str, b: str) -> str | None:
    """The first action class in which the two texts name different poles, e.g. "on/off", or None."""
    wa = _with_particles([_stem(t) for t in _tokens(a)])
    wb = _with_particles([_stem(t) for t in _tokens(b)])
    pa, pb = _poles(wa, _OPPOSITES), _poles(wb, _OPPOSITES)
    for cls in OPPOSITES:
        if cls in pa and cls in pb and pa[cls] != pb[cls]:
            return cls
    sa, sb = set(wa), set(wb)
    for x, y in ((sa, sb), (sb, sa)):
        for t in sorted(x - y):
            for p in _PREFIXES:
                base = t[len(p) :]
                if t.startswith(p) and len(base) >= _MIN_BASE and base in y - x:
                    return f"{base}/{t}"
    return None


def different_entity(a: str, b: str) -> str | None:
    """The first entity kind both texts name with different values ("account: checking/savings"), or None."""
    pa, pb = _poles(_tokens(a), _ENTITIES), _poles(_tokens(b), _ENTITIES)
    for cls in ENTITIES:
        if cls in pa and cls in pb and pa[cls] != pb[cls]:
            return f"{cls}: {'+'.join(pa[cls])}/{'+'.join(pb[cls])}"
    na, nb = names(a), names(b)
    ta, tb = set(_tokens(a)) | set(_WORD.findall(a.casefold())), set(_tokens(b)) | set(_WORD.findall(b.casefold()))
    only_a, only_b = sorted(n for n in na if n not in tb), sorted(n for n in nb if n not in ta)
    if only_a and only_b:
        return f"name: {only_a[0]}/{only_b[0]}"
    return None


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9']*")
_NOT_NAMES = {"i", "i'm", "i'd", "i'll", "i've", "ok", "okay"}


def names(text: str) -> set[str]:
    """Capitalised words that do not start a sentence: branch, product, brand and place names, acronyms."""
    out = set()
    for m in _NAME.finditer(text.replace("\u2019", "'")):
        w = m.group()
        before = text[: m.start()].rstrip()
        if not w[0].isupper() or len(w) < 2 or not before or before[-1] in '.?!:;"(':
            continue
        w = w.casefold().removesuffix("'s")
        if w not in _NOT_NAMES:
            out.add(w)
    return out


GUARDS = ("numbers", "negation", "opposites", "entities")
GUARDS_V1 = ("digits", "negation")  # the guards before the opposite-action and entity checks (reproduces old figures)


def guard(
    a: str,
    b: str,
    check_numbers: bool = True,
    check_negation: bool = True,
    check_opposites: bool = True,
    check_entities: bool = True,
    checks: Sequence[str] | None = None,
) -> str | None:
    """Why two texts must not share an answer despite similar wording, or None. Similarity plays no part."""
    if checks is None:
        checks = [
            n
            for n, on in zip(GUARDS, (check_numbers, check_negation, check_opposites, check_entities), strict=True)
            if on
        ]
    if "digits" in checks and sorted(n.replace(",", "") for n in _NUMBER.findall(a)) != sorted(
        n.replace(",", "") for n in _NUMBER.findall(b)
    ):
        return "numbers differ"
    if "numbers" in checks and numbers(a) != numbers(b):
        return "numbers differ"
    if "negation" in checks and (negations(a) > 0) != (negations(b) > 0):
        return "negation differs"
    if "opposites" in checks and (why := opposite_action(a, b)):
        return f"opposite action ({why})"
    if "entities" in checks and (why := different_entity(a, b)):
        return f"different {why}"
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
    check_opposites: bool = True
    check_entities: bool = True
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
        """The most similar entry at or above the threshold that no guard refuses.

        Guards are checked on every candidate at or above the threshold, best first, so a refused best match
        neither serves its answer nor hides a second entry that does mean the same thing.
        """
        part = self._live(partition)
        scored = sorted(((cosine(vector, e.vector), k, e) for k, e in part.items()), key=lambda x: -x[0])
        if not scored:
            return Lookup(False)
        best_sim, _, best_e = scored[0]
        if best_sim < self.threshold:
            return Lookup(False, best_sim, best_e, "below_threshold")
        refused: str | None = None
        for sim, k, e in scored:
            if sim < self.threshold:
                break
            why = guard(text, e.text, self.check_numbers, self.check_negation, self.check_opposites,
                        self.check_entities)  # fmt: skip
            if why:
                refused = refused or why
                continue
            part.move_to_end(k)
            e.hits += 1
            return Lookup(True, sim, e, "hit")
        return Lookup(False, best_sim, best_e, f"guard: {refused}")

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
    guards: Sequence[str] = GUARDS,
) -> list[dict]:
    """For each threshold: how many same-meaning pairs would hit (hit rate) and how many hits are wrong.

    false_hit_rate = wrong hits / all hits (what a caller experiences); fpr = wrong hits / different-meaning pairs.
    `guards` names the guard set (GUARDS, or GUARDS_V1 for the figures from before the opposite-action and entity
    guards); use_guards=False measures the embedder alone.
    """
    scored = []
    for p in pairs:
        sim = cosine(embedder.embed(p.a), embedder.embed(p.b))
        blocked = guard(p.a, p.b, checks=guards) is not None if use_guards else False
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
                "guarded_same": sum(1 for sim, blocked, same in scored if sim >= t and blocked and same),
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
    heldout: Sequence[Pair] | None = None,
    guards: Sequence[str] = GUARDS,
) -> dict:
    """Pick a threshold on the calibration half of the development pairs, then measure it where it was not picked.

    The development half that played no part in the pick is reported, and so is `heldout`, a separate pair set
    written after the guards' development pairs had been read (evals/semcache_heldout.jsonl): it is the honest
    estimate, because nothing in the guards or the threshold was designed on it.
    """
    emb = embedder or HashingEmbedder()
    cal, hold = split(pairs)
    cal_rows = calibrate(cal, emb, grid or threshold_grid(), guards=guards)
    pick = pick_threshold(cal_rows, target, use_upper_bound=conservative)
    out = {
        "embedder": getattr(emb, "name", type(emb).__name__),
        "guards": list(guards),
        "pairs": {"total": len(pairs), "calibration": len(cal), "holdout": len(hold)},
        "positives": {"calibration": sum(p.same for p in cal), "holdout": sum(p.same for p in hold)},
        "target_false_hit_rate": target,
        "rule": "95% upper bound meets target" if conservative else "point estimate meets target",
        "calibration_grid": cal_rows,
        "chosen": pick,
        "holdout_at_chosen": None,
        "holdout_without_guards_at_chosen": None,
        "heldout": None,
    }
    if pick:
        t = [pick["threshold"]]
        out["holdout_at_chosen"] = calibrate(hold, emb, t, guards=guards)[0]
        out["holdout_without_guards_at_chosen"] = calibrate(hold, emb, t, use_guards=False)[0]
    if heldout is not None:
        out["heldout"] = {
            "pairs": len(heldout),
            "positives": sum(p.same for p in heldout),
            "at_chosen": calibrate(heldout, emb, [pick["threshold"]], guards=guards)[0] if pick else None,
            "without_guards_at_chosen": calibrate(heldout, emb, [pick["threshold"]], use_guards=False)[0]
            if pick
            else None,
        }
    return out

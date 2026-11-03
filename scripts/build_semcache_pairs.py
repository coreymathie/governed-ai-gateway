# Corey Mathie, 2026
"""
Write evals/semcache_pairs.jsonl: the 160 hand-written semantic-cache calibration pairs plus generated
near-duplicate and lookalike pairs, all fictional Cypress Harbor Credit Union member questions.

The hand-written pairs (evals/semcache_pairs_handwritten.jsonl) come first, unchanged. The generated pairs
come from question frames: each frame has several phrasings and slots (a fee, an action and its opposite, a
product, a period, a day). Two questions with the same slot values ask the same thing (same=true): either one
phrasing with the noise members add (lower case, no question mark, a greeting, a typo, a contraction) or two
different phrasings. Two questions that differ in one slot value are lookalikes (same=false): "turn on" and
"turn off", an incoming and an outgoing wire, the APY and the dividend rate, Saturday and Sunday closing time.
Slot values within a frame are chosen so that a different value always means a different answer.

The calibration/holdout split is by a hash of each pair id (router/semcache.py), so adding pairs does not move
the hand-written pairs between halves.

    python scripts/build_semcache_pairs.py            # write the file
    python scripts/build_semcache_pairs.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDWRITTEN = ROOT / "evals" / "semcache_pairs_handwritten.jsonl"
OUT = ROOT / "evals" / "semcache_pairs.jsonl"
SEED = 20261103
SAME, DIFFERENT = 600, 600  # generated pairs of each label

# Each frame: phrasings with {slot} fields, and for each slot the values it takes. Within a slot, every value
# leads to a different answer, so changing one value makes a lookalike that must not share a cached answer.
FRAMES = [
    {
        "phrasings": ["What is the fee for {x}?", "How much does {x} cost?", "What do you charge for {x}?",
                      "How much is the fee for {x}?"],
        "slots": {"x": ["an outgoing domestic wire", "an outgoing international wire", "an incoming domestic wire",
                        "an incoming international wire", "expedited card delivery", "a replacement debit card",
                        "paper statements", "an overdraft", "skip-a-pay", "an out-of-network ATM withdrawal",
                        "a cashier's check", "a stop payment", "a returned deposited check", "a notary stamp",
                        "a money order", "a safe deposit box"]},
    },
    {
        "phrasings": ["How do I {act} {obj}?", "How can I {act} {obj}?", "What's the way to {act} {obj}?",
                      "Can you tell me how to {act} {obj}?", "What are the steps to {act} {obj}?"],
        "combos": [
            (["lock", "unlock"], ["my debit card", "my credit card"]),
            (["turn on", "turn off"], ["two-step verification", "text alerts", "paperless statements",
                                       "overdraft coverage", "biometric sign-in", "quick balance"]),
            (["add", "remove"], ["a joint owner", "a beneficiary", "an authorized user"]),
            (["open", "close"], ["a savings account", "a checking account", "a Money Market account",
                                 "a Holiday Club account"]),
            (["set up", "cancel"], ["direct deposit", "automatic loan payments", "a travel notice",
                                    "a recurring transfer"]),
        ],
    },
    {
        "phrasings": ["What is the {m} on {p}?", "What's the current {m} for {p}?", "Tell me the {m} on {p}.",
                      "What {m} do you pay on {p}?"],
        "slots": {"m": ["APY", "dividend rate"],
                  "p": ["the 6-month certificate", "the 12-month certificate", "the 18-month certificate",
                        "the 36-month certificate", "the Money Market account"]},
    },
    {
        "phrasings": ["What is the {period} limit for {svc}?", "What's my {period} limit on {svc}?",
                      "How high is the {period} limit for {svc}?", "Tell me the {period} limit for {svc}."],
        "slots": {"period": ["daily", "monthly"],
                  "svc": ["Zelle", "mobile check deposit", "ATM withdrawals", "debit card purchases",
                          "external transfers"]},
    },
    {
        "phrasings": ["What time does the {b} branch close on {d}?", "When does the {b} branch close on {d}?",
                      "How late is the {b} branch open on {d}?"],
        "fixed": {"b": ["Davie", "Weston", "Plantation", "Coral Springs", "Boca Raton", "Miramar", "Sunrise"]},
        "slots": {"d": ["Saturday", "Sunday", "a weekday"]},
    },
    {
        "phrasings": ["What is the rate on a {cond} {loan}?", "What rate do you offer on a {cond} {loan}?",
                      "What's the APR for a {cond} {loan}?"],
        "slots": {"cond": ["new", "used"], "loan": ["auto loan", "boat loan", "RV loan", "motorcycle loan"]},
    },
    {
        "phrasings": ["Where can I find my {doc}?", "Where do I get my {doc}?", "How do I download my {doc}?"],
        "slots": {"doc": ["monthly statement", "direct deposit form", "year-end loan interest summary",
                          "privacy notice", "fee schedule", "dividend tax form", "account agreement"]},
    },
    {
        "phrasings": ["How do I report {thing}?", "Who do I contact to report {thing}?",
                      "What should I do to report {thing}?"],
        "slots": {"thing": ["a lost debit card", "an unauthorized ACH debit",
                            "a Zelle payment sent to the wrong person", "a deceased member", "a change of address",
                            "identity theft"]},
    },
    {
        "phrasings": ["Can I {act} at {obj}?", "Is it possible to {act} at {obj}?", "Am I able to {act} at {obj}?"],
        "combos": [
            (["deposit cash", "withdraw cash"], ["a CO-OP network ATM", "a CO-OP shared branch"]),
            (["make a loan payment", "open a new account"], ["a CO-OP shared branch", "a Cypress Harbor branch"]),
        ],
    },
    {
        "phrasings": ["How long does {x} take?", "How many days does {x} take?", "When should I expect {x}?"],
        "slots": {"x": ["a replacement debit card", "expedited card delivery", "an outgoing domestic wire",
                        "an outgoing international wire", "a new box of checks", "a mailed paper statement",
                        "a loan payoff letter"]},
    },
    # Longer messages, as members type them in chat: one changed word is a smaller share of the text, so
    # lookalikes score higher with a lexical embedder.
    {
        "phrasings": [
            "Hi, I have a question about my account. I would like to {act} {obj}, can you walk me through how to do "
            "that in the mobile app?",
            "I'm trying to {act} {obj} in the mobile app but I can't find where to do it. Can you tell me the steps?",
            "Could you explain how I {act} {obj} from my phone? I looked in the app settings and did not see it.",
        ],
        "combos": [
            (["turn on", "turn off"], ["two-step verification", "text alerts", "paperless statements",
                                       "biometric sign-in"]),
            (["lock", "unlock"], ["my debit card", "my credit card"]),
            (["set up", "cancel"], ["a recurring transfer to savings", "automatic loan payments"]),
        ],
    },
    {
        "phrasings": [
            "My daughter is starting college in the fall and I want to {act} her as {obj} on my checking account. "
            "What do I need to bring to the branch?",
            "I need to {act} my daughter as {obj} on my checking account now that she is in college. What do I "
            "have to bring with me to a branch?",
        ],
        "combos": [(["add", "remove"], ["a joint owner", "an authorized signer", "the payable-on-death beneficiary"])],
    },
    {
        "phrasings": [
            "I was looking at the rate sheet on your website and wanted to confirm the {m} on {p} before I open one "
            "this week.",
            "Before I open one this week, can you confirm the {m} on {p}? I saw it on the rate sheet on your "
            "website.",
        ],
        "slots": {"m": ["APY", "dividend rate", "minimum deposit", "early withdrawal penalty"],
                  "p": ["the 12-month certificate", "the Money Market account"]},
    },
    {
        "phrasings": [
            "I'm going on a trip next month and need to know the {period} limit for {svc} so my transactions don't "
            "get declined while I'm away.",
            "So nothing gets declined while I'm traveling next month, what is the {period} limit for {svc}?",
        ],
        "slots": {"period": ["daily", "monthly"],
                  "svc": ["ATM withdrawals", "debit card purchases", "Zelle", "external transfers"]},
    },
    {
        "phrasings": [
            "I need to send money to my landlord before the end of the day. What is the fee for {x}, and how long "
            "does it take to arrive?",
            "What does {x} cost and how long does it take to arrive? I need to pay my landlord before the end of "
            "the day.",
        ],
        "slots": {"x": ["an outgoing domestic wire", "an outgoing international wire", "a cashier's check",
                        "a Zelle payment", "an external transfer"]},
    },
]  # fmt: skip

GREETINGS = ["Hi, ", "Hello, ", "Hey, ", "Quick question: ", "Good morning. ", "Hi there, "]
THANKS = [" Thanks!", " Thank you.", " Thanks", " thx"]
CONTRACTIONS = [("What is", "What's"), ("How do I", "How do i"), ("Can you", "Could you"), ("What are", "What're")]


def fill(phrasing: str, values: dict) -> str:
    return phrasing.format(**values)


def noise(rng: random.Random, text: str) -> str:
    """One of the changes members make when they ask the same thing again."""
    kind = rng.randrange(6)
    if kind == 0:
        return text.lower().rstrip("?.")
    if kind == 1:
        return rng.choice(GREETINGS) + text[0].lower() + text[1:]
    if kind == 2:
        return text.rstrip("?.") + "?" + rng.choice(THANKS)
    if kind == 3:
        for a, b in CONTRACTIONS:
            if a in text:
                return text.replace(a, b, 1)
            if b in text:
                return text.replace(b, a, 1)
        return text.rstrip("?.")
    if kind == 4:  # a typo: two letters swapped in the longest word
        words = text.split(" ")
        i = max(range(len(words)), key=lambda k: len(words[k].strip("?.,'")))
        w = words[i]
        j = rng.randrange(1, max(2, len(w.strip("?.,")) - 2))
        words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
        return " ".join(words)
    return text.replace("?", " ?").replace(".", " .") if rng.random() < 0.5 else "  " + text + "  "


def frame_values(frame: dict) -> list[tuple[dict, list[str]]]:
    """Every slot assignment of a frame, with the names of the slots a lookalike may change."""
    out = []
    if "combos" in frame:
        for acts, objs in frame["combos"]:
            for a in acts:
                for o in objs:
                    out.append(({"act": a, "obj": o}, ["act", "obj"], {"act": acts, "obj": objs}))
        return out
    names = list(frame["slots"])
    fixed = frame.get("fixed", {})
    grid = [{}]
    for name in names:
        grid = [{**g, name: v} for g in grid for v in frame["slots"][name]]
    for f in fixed:
        grid = [{**g, f: v} for g in grid for v in fixed[f]]
    return [(g, names, frame["slots"]) for g in grid]


def generate(rng: random.Random) -> list[dict]:
    same, different = [], []
    for fi, frame in enumerate(FRAMES):
        phrasings = frame["phrasings"]
        for values, changeable, choices in frame_values(frame):
            for pi, ph in enumerate(phrasings):
                a = fill(ph, values)
                # same meaning: noise on the same phrasing, and a different phrasing (sometimes with noise too)
                same.append((fi, a, noise(rng, a)))
                other = phrasings[(pi + 1 + rng.randrange(len(phrasings) - 1)) % len(phrasings)]
                b = fill(other, values)
                same.append((fi, a, noise(rng, b) if rng.random() < 0.3 else b))
                # lookalike: one slot value changed, usually in the same phrasing
                slot = rng.choice(changeable)
                alt = rng.choice([v for v in choices[slot] if v != values[slot]])
                b2 = fill(ph if rng.random() < 0.8 else other, {**values, slot: alt})
                different.append((fi, a, noise(rng, b2) if rng.random() < 0.3 else b2))

    # Drop exact duplicates, then take a fixed number of each label, spread over the frames.
    def norm(text: str) -> str:
        return " ".join("".join(ch for ch in text.casefold() if ch.isalnum() or ch == " ").split())

    def pick(pool: list, n: int) -> list:
        seen, uniq = set(), []
        for fi, a, b in pool:
            key = frozenset((norm(a), norm(b)))  # a pair, its reverse and its re-noised copy count once
            if a != b and key not in seen:
                seen.add(key)
                uniq.append((fi, a, b))
        rng.shuffle(uniq)
        return uniq[:n]

    out = []
    for k, (_fi, a, b) in enumerate(pick(same, SAME), 1):
        out.append({"id": f"gs{k:04d}", "a": a, "b": b, "same": True})
    for k, (_fi, a, b) in enumerate(pick(different, DIFFERENT), 1):
        out.append({"id": f"gd{k:04d}", "a": a, "b": b, "same": False})
    return out


def render() -> str:
    handwritten = [line for line in HANDWRITTEN.read_text().splitlines() if line.strip() and not line.startswith("#")]
    head = [
        "# Labelled pairs for semantic-cache calibration (scripts/calibrate_semcache.py). "
        "Fictional credit-union questions.",
        "# same=true: one cached answer serves both. same=false: reusing the answer would be wrong.",
        f"# Written by scripts/build_semcache_pairs.py: {len(handwritten)} hand-written pairs (s*, d*), then generated "
        "near-duplicates (gs*) and lookalikes (gd*).",
    ]
    generated = [json.dumps(p, ensure_ascii=False) for p in generate(random.Random(SEED))]
    return "\n".join(head + handwritten + generated) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/build_semcache_pairs.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(1 for line in text.splitlines() if line.startswith('{'))} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

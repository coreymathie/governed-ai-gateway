# Corey Mathie, 2026
"""
Write evals/semcache_heldout.jsonl: a held-out semantic-cache pair set, kept apart from the development set
(evals/semcache_pairs.jsonl) and never used to choose a threshold or to design a guard.

Why it exists: both halves of the development set were read while the semantic-cache guards were designed, so
neither half is an honest test any more. This set was written afterwards, before the guards changed, with its
own seed and its own question frames, phrasings, entities and noise (none of them are copies of the
development frames). It mixes true paraphrases with lookalikes of several kinds, including kinds no guard in
router/semcache.py looks for: an action and its opposite, a swapped account, card, loan or document, a
different branch, day, month, country, currency or device, a different amount, "over" versus "under", and a
different question about the same thing (the fee versus the processing time).

Each frame has phrasings with {slot} fields. Two questions with the same slot values ask the same thing
(same=true): the same phrasing with the noise members add, the same phrasing with a synonym for the action, or
a different phrasing. Two questions that differ in one slot value are lookalikes (same=false); within a slot,
every value leads to a different answer.

    python scripts/build_semcache_heldout.py            # write the file
    python scripts/build_semcache_heldout.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "semcache_heldout.jsonl"
SEED = 41177  # unrelated to the development set's seed
SAME, DIFFERENT = 1400, 1400  # pairs of each label

# Synonyms a member might use for the same action (same=true when swapped in). Only listed where the meaning
# really is the same for the answer.
SYNONYMS = {
    "freeze": ["lock", "temporarily block"],
    "unfreeze": ["unlock", "unblock"],
    "enroll in": ["sign up for", "join"],
    "unenroll from": ["leave", "get out of"],
    "start": ["begin", "set up"],
    "stop": ["end", "cancel"],
    "add": ["name", "list"],
    "remove": ["drop"],
}

FRAMES = [
    {  # card controls
        "phrasings": ["Is there a way to {act} my {card} from the app?", "I want to {act} my {card} right now, how?",
                      "Can I {act} my {card} myself or do I have to call?",
                      "Where in online banking do I {act} my {card}?"],
        "combos": [
            (["freeze", "unfreeze"], ["Visa debit card", "Visa credit card", "business debit card", "HSA debit card"]),
            (["activate", "replace"], ["Visa debit card", "Visa credit card", "business debit card"]),
            (["report a lost", "order a second"], ["Visa debit card", "HSA debit card"]),
        ],
    },
    {  # enrollments
        "phrasings": ["How would I {act} {svc}?", "I'd like to {act} {svc}. What do I do?",
                      "Can you help me {act} {svc}?", "What's the process to {act} {svc}?"],
        "combos": [
            (["enroll in", "unenroll from"], ["e-statements", "account alerts by email", "bill pay",
                                              "the round-up savings program"]),
            (["sign up for", "opt out of"], ["courtesy pay", "the skip-a-payment program", "paper check images"]),
            (["start", "stop"], ["payroll deduction into savings", "automatic transfers to my HSA",
                                 "a monthly transfer to my son's account"]),
        ],
    },
    {  # people on an account
        "phrasings": ["What paperwork do I need to {act} my husband as {role} on my {acct}?",
                      "Which forms are needed to {act} my husband as {role} on my {acct}?",
                      "If I want to {act} my husband as {role} on my {acct}, what do you need from me?"],
        "combos": [
            (["add", "remove"], ["a co-owner", "a trusted contact", "an agent under power of attorney",
                                   "a transfer-on-death beneficiary"]),
        ],
        "extra": {"acct": ["savings account", "share certificate", "Roth IRA"]},
    },
    {  # deposit product terms
        "phrasings": ["Has the {m} on your {p} changed recently?", "Do you know the current {m} for the {p}?",
                      "I'm comparing credit unions: what {m} does Cypress Harbor have on the {p}?",
                      "Can you look up the {m} on the {p} for me?"],
        "slots": {"m": ["APY", "minimum opening deposit", "early-withdrawal penalty", "compounding schedule"],
                  "p": ["24-month share certificate", "jumbo certificate", "youth savings account",
                        "traditional IRA certificate"]},
    },
    {  # caps by period
        "phrasings": ["Is there a cap on how much I can {x} {per}?", "How much am I allowed to {x} {per}?",
                      "What's the most I can {x} {per}?"],
        "slots": {"x": ["withdraw from an ATM", "send with Zelle", "deposit by mobile", "spend on my debit card"],
                  "per": ["per day", "per week", "per month", "in one transaction"]},
    },
    {  # branch hours
        "phrasings": ["Is the {b} branch open on {d}?", "Are you open on {d} at the {b} branch?",
                      "What are the {b} branch's hours on {d}?"],
        "slots": {"b": ["Harborview", "Pinecrest", "Lakeside", "Bayfront", "Northgate", "Mill Creek"],
                  "d": ["Saturday mornings", "Sunday", "Good Friday", "Columbus Day", "the day after Thanksgiving",
                        "weekday evenings"]},
    },
    {  # funds availability
        "phrasings": ["When will {x} be available to spend?", "How soon can I use the money from {x}?",
                      "How long is the hold on {x}?"],
        "slots": {"x": ["an incoming ACH transfer", "a direct deposit", "a check I deposited at the ATM",
                        "a mobile check deposit", "a wire from overseas", "a wire from another U.S. bank",
                        "a cash deposit at a shared branch"]},
    },
    {  # loan payments
        "phrasings": ["Can I {act} on my {loan} this month?", "Is it okay to {act} on my {loan} this month?",
                      "Would you let me {act} on my {loan} this month?"],
        "slots": {"act": ["skip a payment", "make an extra payment", "pay only the interest", "pay early"],
                  "loan": ["auto loan", "mortgage", "personal loan", "HELOC", "boat loan"]},
    },
    {  # cash reporting thresholds
        "phrasings": ["Does a cash deposit {cmp} $10,000 get reported to the government?",
                      "If I bring in cash {cmp} $10,000, do you have to report it?",
                      "Is a cash deposit {cmp} $10,000 something you report?"],
        "slots": {"cmp": ["over", "under", "of exactly"]},
    },
    {
        "phrasings": ["If I deposit {amt} in cash, does that get reported?",
                      "Will a {amt} cash deposit be reported by the credit union?"],
        "slots": {"amt": ["$3,000", "$9,500", "$10,000", "$12,000", "twenty thousand dollars"]},
    },
    {  # tax and loan documents
        "phrasings": ["Where's my {doc} for 2025?", "How do I get a copy of my {doc} for 2025?",
                      "Has my {doc} for 2025 been mailed yet?"],
        "slots": {"doc": ["1099-INT", "1098 mortgage interest statement", "IRA contribution summary",
                          "escrow analysis", "HSA contribution form"]},
    },
    {  # devices
        "phrasings": ["Does the mobile app support {feat} on {dev}?", "Can I use {feat} in the app on {dev}?",
                      "Is {feat} available on {dev} in your app?"],
        "slots": {"feat": ["Face ID", "fingerprint login", "check deposit", "dark mode"],
                  "dev": ["iPhone", "Android", "iPad", "Apple Watch"]},
    },
    {  # currency and travel
        "phrasings": ["Can I order {cur} at a branch before my trip?", "Do branches sell {cur} for travel?",
                      "Where can I pick up {cur} before I travel?"],
        "slots": {"cur": ["euros", "Canadian dollars", "Japanese yen", "Mexican pesos", "British pounds"]},
    },
    {
        "phrasings": ["I'm traveling to {country} in {month}. Do I need to tell you before I use my card there?",
                      "Should I let the credit union know I'll be in {country} in {month} so my card works?"],
        "slots": {"country": ["Canada", "Mexico", "Japan", "Italy"], "month": ["March", "July", "December"]},
    },
    {  # a different question about the same thing
        "phrasings": ["What's the {q} for a {x}?", "Tell me the {q} on a {x}.", "Could you share the {q} for a {x}?"],
        "slots": {"q": ["fee", "processing time", "daily cutoff time", "maximum amount"],
                  "x": ["same-day ACH transfer", "bill pay check", "person-to-person payment"]},
    },
    # Longer chat messages: one changed slot is a small share of the text.
    {
        "phrasings": [
            "Hey, quick one. I just started a new job and my first paycheck comes next Friday. I want {split} of "
            "it to go straight into my {acct}. Is there a form I fill out for that?",
            "I just started a new job and want {split} of each paycheck to land in my {acct} automatically, "
            "starting next Friday. What form do I need for that?",
        ],
        "slots": {"split": ["all", "half", "a fixed amount"],
                  "acct": ["checking account", "savings account", "Christmas Club account"]},
    },
    {
        "phrasings": [
            "I'm looking at my statement and there's a {thing} from the hardware store from last week. What's the "
            "next step to sort it out?",
            "There's a {thing} from the hardware store on last week's statement. How do I get that sorted out?",
        ],
        "slots": {"thing": ["duplicate charge", "pending charge that is stuck", "refund that has not posted",
                            "charge for the wrong amount"]},
    },
    {
        "phrasings": [
            "My mom is moving in with us and I want to {act} her {as} on my checking account so she can help with "
            "the bills. Can we do it online or does she have to come into a branch with me?",
            "Can we {act} my mom {as} on my checking account online, or does she need to come to a branch with "
            "me? She is moving in and will help with the bills.",
        ],
        "combos": [(["add", "remove"], ["as a joint owner", "as an authorized signer"])],
    },
]  # fmt: skip

OPENERS = ["Hey there, ", "Hi team, ", "Morning! ", "Question: ", "Hello again, ", "Sorry to bother you, "]
CLOSERS = [" Appreciate it.", " Thanks so much", " ty", " Thank you!"]


def noise(rng: random.Random, text: str) -> str:
    """One way a member types the same question again (different from the development set's noise)."""
    kind = rng.randrange(7)
    if kind == 0:  # all lower case, no apostrophes or final punctuation, first article dropped
        low = text.lower().replace("'", "").rstrip("?.!")
        for art in (" the ", " a ", " an "):
            if art in low:
                return low.replace(art, " ", 1)
        return low + " pls"
    if kind == 1:
        return rng.choice(OPENERS) + text[0].lower() + text[1:]
    if kind == 2:
        return text.rstrip("?.!") + rng.choice(["?", "??", "."]) + rng.choice(CLOSERS)
    if kind == 3:  # "please" before the first verb-ish word after the question word
        words = text.split(" ")
        return " ".join([*words[:2], "please", *words[2:]]) if len(words) > 3 else text + " please"
    if kind == 4:  # a dropped letter in the longest word
        words = text.split(" ")
        i = max(range(len(words)), key=lambda k: len(words[k].strip("?.,!'")))
        w = words[i]
        j = rng.randrange(1, max(2, len(w.strip("?.,!")) - 1))
        words[i] = w[:j] + w[j + 1 :]
        return " ".join(words)
    if kind == 5:  # texting shorthand
        return text.replace("you", "u").replace("your", "ur").replace("Can I", "can i").replace("I'm", "im")
    return text.replace(" the ", " the the ", 1) if " the " in text else "um, " + text[0].lower() + text[1:]


def frame_values(frame: dict) -> list[tuple[dict, list[str], dict]]:
    """Every slot assignment of a frame, the slots a lookalike may change, and each slot's values."""
    if "combos" in frame:
        out = []
        extra = frame.get("extra", {})
        for acts, objs in frame["combos"]:
            obj_key = next(k for k in ("role", "as", "card", "svc") if f"{{{k}}}" in frame["phrasings"][0])
            grid = [{"act": a, obj_key: o} for a in acts for o in objs]
            for name, vals in extra.items():
                grid = [{**g, name: v} for g in grid for v in vals]
            choices = {"act": acts, obj_key: objs, **extra}
            out += [(g, list(choices), choices) for g in grid]
        return out
    names = list(frame["slots"])
    grid = [{}]
    for name in names:
        grid = [{**g, name: v} for g in grid for v in frame["slots"][name]]
    return [(g, names, frame["slots"]) for g in grid]


def generate(rng: random.Random) -> list[dict]:
    same, different = [], []
    for frame in FRAMES:
        phrasings = frame["phrasings"]
        for values, changeable, choices in frame_values(frame):
            for pi, ph in enumerate(phrasings):
                a = ph.format(**values)
                same.append((a, noise(rng, a)))
                same.append((noise(rng, a), a if rng.random() < 0.5 else noise(rng, a)))
                other = phrasings[(pi + 1 + rng.randrange(len(phrasings) - 1)) % len(phrasings)]
                b = other.format(**values)
                same.append((a, noise(rng, b) if rng.random() < 0.4 else b))
                same.append((noise(rng, b), noise(rng, a)))
                if values.get("act") in SYNONYMS:
                    syn = {**values, "act": rng.choice(SYNONYMS[values["act"]])}
                    same.append((a, (ph if rng.random() < 0.5 else other).format(**syn)))
                for _ in range(3):  # three lookalikes per question: one slot value changed
                    slot = rng.choice(changeable)
                    alt = rng.choice([v for v in choices[slot] if v != values[slot]])
                    b2 = (ph if rng.random() < 0.75 else other).format(**{**values, slot: alt})
                    different.append((a, noise(rng, b2) if rng.random() < 0.35 else b2))

    def norm(text: str) -> str:
        return " ".join("".join(ch for ch in text.casefold() if ch.isalnum() or ch == " ").split())

    def pick(pool: list, n: int) -> list:
        seen, uniq = set(), []
        for a, b in pool:
            key = frozenset((norm(a), norm(b)))
            if norm(a) != norm(b) and key not in seen:
                seen.add(key)
                uniq.append((a, b))
        rng.shuffle(uniq)
        if len(uniq) < n:
            raise SystemExit(f"only {len(uniq)} unique pairs for a target of {n}")
        return uniq[:n]

    out = [{"id": f"hs{k:04d}", "a": a, "b": b, "same": True} for k, (a, b) in enumerate(pick(same, SAME), 1)]
    diff = pick(different, DIFFERENT)
    out += [{"id": f"hd{k:04d}", "a": a, "b": b, "same": False} for k, (a, b) in enumerate(diff, 1)]
    return out


def render() -> str:
    head = [
        "# Held-out semantic-cache pairs (scripts/build_semcache_heldout.py). Fictional credit-union questions.",
        "# Never used to pick a threshold or to design a guard; the development set is evals/semcache_pairs.jsonl.",
        "# same=true: one cached answer serves both. same=false: reusing the answer would be wrong.",
    ]
    return "\n".join(head + [json.dumps(p, ensure_ascii=False) for p in generate(random.Random(SEED))]) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/build_semcache_heldout.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(1 for line in text.splitlines() if line.startswith('{'))} pairs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

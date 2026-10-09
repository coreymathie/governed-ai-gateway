# Corey Mathie, 2026
"""
Cypress Harbor Credit Union (fictional): the one definition of the sample company's applications.

Every place the console tells the credit union's story reads this module: the usage generator
(scripts/generate_sample_company.py), the request-log generator (scripts/sample_requests.py), the
in-browser engine's keys and traffic (demo/engine.py) and the consistency tests. Route aliases and
their deployments come from config/routes.yaml (the credit union's route config); budgets and per-key
caps come from its `budgets:` section. This module holds what a routes file does not: which team owns
each application, which alias it calls, how big its prompts are, when it runs and how much it is used.

Nothing here is a measurement. Volumes, token sizes and cache hit rates are stated assumptions for a
credit union of about 92,000 members. Pure Python with no imports, so Pyodide loads it as is.
"""

COMPANY = {
    "name": "Cypress Harbor Credit Union",
    "short": "Cypress Harbor CU",
    "fictional": True,
    "industry": "Credit union (financial services)",
    "headquarters": "Fort Lauderdale, Florida",
    "members": 92400,
    "assets_usd": 1_400_000_000,
    "employees": 340,
    "branches": 11,
    "providers": ["Anthropic", "OpenAI", "Google Gemini", "On-prem Llama (Ollama)"],
    "regulators": ["NCUA", "Florida OFR"],
    "consumer_rules": "CFPB rules (Regulation E, Regulation Z) apply; CFPB examines institutions over $10B in "
    "assets, so the credit union is examined by NCUA and the Florida Office of Financial Regulation.",
}

TEAMS = {
    "member-services": "Member services",
    "digital-banking": "Digital banking",
    "risk-analytics": "Risk analytics",
    "lending": "Lending",
    "compliance": "Compliance",
    "it-engineering": "IT and engineering",
    "marketing": "Marketing",
}

# USD per 1M tokens (input, output): list prices; on-prem Llama at an assumed GPU chargeback rate. The same
# values as evals/sim_profiles.json and config/routes.yaml `prices:` (tests/test_sample_consistency.py checks).
PRICES = {
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "anthropic/claude-sonnet-4-5": (3.00, 15.00),
    "openai/gpt-4.1": (2.00, 8.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "gemini/gemini-2.5-flash": (0.30, 2.50),
    "ollama/llama3.1:8b": (0.05, 0.05),
}

# Share of fast-chat requests each deployment answers first (strategy: latency ranks whichever has been
# fastest lately), in the order the alias lists its targets.
LATENCY_MIX = {"fast-chat": (0.45, 0.35, 0.20)}

# Relative traffic by local hour (Eastern). "night" is the fraud-alert batch window, 1:00 to 4:59 am.
HOURS = {
    "always": tuple(0.25 if h < 6 else 0.5 if h < 8 else 1.3 if 18 <= h <= 21 else 1.0 for h in range(24)),
    "branch": tuple(1.0 if 9 <= h < 18 else 0.04 for h in range(24)),
    "office": tuple(1.0 if 8 <= h < 18 else 0.04 for h in range(24)),
    "night": tuple(1.0 if 1 <= h <= 4 else 0.0 for h in range(24)),
}

# app: team, [(alias, share of requests)], {alias: (input tokens, output tokens)} per request, temperature,
# hours, requests on a working day (before growth), weekend share, exact-cache hit rate, who calls it,
# and whether the PII hook runs / the work is regulated (on-prem only).
APPS = {
    "member-assistant": {
        "team": "member-services", "routes": [("smart-fast", 1.0)], "tokens": {"smart-fast": (1800, 260)},
        "temperature": 0.0, "hours": "always", "workday": 3600, "weekend": 0.55, "cache_hit": 0.21,
        "caller": "member", "pii": True, "regulated": False,
    },
    "agent-assist": {
        "team": "member-services", "routes": [("smart-fast", 1.0)], "tokens": {"smart-fast": (2000, 230)},
        "temperature": 0.0, "hours": "branch", "workday": 2100, "weekend": 0.2, "cache_hit": 0.21,
        "caller": "agent", "pii": True, "regulated": False,
    },
    "online-banking": {
        "team": "digital-banking", "routes": [("fast-chat", 1.0)], "tokens": {"fast-chat": (2000, 300)},
        "temperature": 0.0, "hours": "always", "workday": 2600, "weekend": 0.6, "cache_hit": 0.17,
        "caller": "member", "pii": False, "regulated": False,
    },
    "mobile-banking": {
        "team": "digital-banking", "routes": [("fast-chat", 1.0)], "tokens": {"fast-chat": (1900, 280)},
        "temperature": 0.0, "hours": "always", "workday": 2200, "weekend": 0.65, "cache_hit": 0.17,
        "caller": "member", "pii": False, "regulated": False,
    },
    "fraud-alert-narratives": {
        "team": "risk-analytics", "routes": [("cheap-batch", 1.0)], "tokens": {"cheap-batch": (4500, 320)},
        "temperature": 0.0, "hours": "night", "workday": 300, "weekend": 1.0, "cache_hit": 0.0,
        "caller": "batch", "pii": False, "regulated": False,
    },
    "dispute-triage": {
        "team": "risk-analytics", "routes": [("regulated-fast", 1.0)], "tokens": {"regulated-fast": (3000, 400)},
        "temperature": 0.0, "hours": "office", "workday": 250, "weekend": 0.05, "cache_hit": 0.04,
        "caller": "disputes", "pii": True, "regulated": True,
    },
    "loan-doc-extraction": {
        "team": "lending", "routes": [("smart-fast", 0.78), ("heavy-reasoning", 0.22)],
        "tokens": {"smart-fast": (3000, 350), "heavy-reasoning": (5200, 700)},
        "temperature": 0.0, "hours": "office", "workday": 720, "weekend": 0.05, "cache_hit": 0.06,
        "caller": "lending", "pii": False, "regulated": False,
    },
    "reg-change-digest": {
        "team": "compliance", "routes": [("heavy-reasoning", 1.0)], "tokens": {"heavy-reasoning": (6000, 350)},
        "temperature": 0.0, "hours": "office", "workday": 30, "weekend": 0.0, "cache_hit": 0.09,
        "caller": "compliance", "pii": False, "regulated": False,
    },
    "bsa-case-notes": {
        "team": "compliance", "routes": [("regulated-fast", 1.0)], "tokens": {"regulated-fast": (2600, 350)},
        "temperature": 0.0, "hours": "office", "workday": 80, "weekend": 0.0, "cache_hit": 0.0,
        "caller": "bsa", "pii": True, "regulated": True,
    },
    "code-assistant": {
        "team": "it-engineering", "routes": [("local-first", 0.85), ("heavy-reasoning", 0.15)],
        "tokens": {"local-first": (2000, 300), "heavy-reasoning": (3500, 500)},
        "temperature": 0.2, "hours": "office", "workday": 700, "weekend": 0.05, "cache_hit": 0.0,
        "caller": "developer", "pii": False, "regulated": False,
    },
    "content-drafts": {
        "team": "marketing", "routes": [("smart-fast", 1.0)], "tokens": {"smart-fast": (900, 1300)},
        "temperature": 0.7, "hours": "office", "workday": 120, "weekend": 0.02, "cache_hit": 0.0,
        "caller": "marketing", "pii": False, "regulated": False,
    },
}  # fmt: skip

# Who appears as the caller in the request log: member sessions, the nightly job, branch or contact-center
# staff for agent assist, and the department (with a staff role) for back-office apps.
CALLERS = {
    "member": ("member session", None),
    "agent": ("contact center", "agent"),
    "batch": ("nightly job", None),
    "disputes": ("Card disputes", "analyst"),
    "lending": ("Consumer lending", "processor"),
    "compliance": ("Compliance office", "analyst"),
    "bsa": ("BSA office", "analyst"),
    "developer": ("IT engineering", "developer"),
    "marketing": ("Marketing", "writer"),
}
BRANCHES = ["Fort Lauderdale", "Plantation", "Coral Springs", "Pembroke Pines", "Davie", "Sunrise", "Weston",
            "Boca Raton", "Pompano Beach", "Hollywood", "Miramar"]  # fmt: skip

MODEL_NAMES = {
    "anthropic/claude-haiku-4-5": "Claude Haiku 4.5",
    "anthropic/claude-sonnet-4-5": "Claude Sonnet 4.5",
    "openai/gpt-4.1": "GPT-4.1",
    "openai/gpt-4.1-mini": "GPT-4.1 mini",
    "openai/gpt-4.1-nano": "GPT-4.1 nano",
    "gemini/gemini-2.5-flash": "Gemini 2.5 Flash",
    "ollama/llama3.1:8b": "Llama 3.1 8B (on-prem)",
}

# The in-browser engine's app keys (Budgets & Keys, the traffic generator, the runaway scenario).
ENGINE_KEYS = ["online-banking", "mobile-banking", "member-assistant", "fraud-alert-narratives", "code-assistant"]

# The demo engine runs a few dozen requests where the credit union runs thousands an hour, so its caps and
# 7-day anomaly baselines are the credit union's divided by this factor (shown on Budgets & Keys).
ENGINE_SCALE = 25

# A vendor developer's key: team "contractors" in config/policies.yaml, working on IT's code-assistant.
CONTRACTOR = {"key": "vendor-code-assist", "team": "contractors", "app": "code-assistant", "caller": "vendor developer"}

# The September 24 runaway: an engineer's coding agent stuck in a fix-and-retest loop on heavy-reasoning.
# Each call carries the growing conversation, so it is larger than a usual code-assistant request.
RUNAWAY = {
    "date": "2026-09-24", "key": "code-assistant", "alias": "heavy-reasoning", "start": "14:06",
    "calls_per_hour": 1200, "tokens": (9000, 600), "noticed": "08:00",
}  # fmt: skip

# Provider incidents the fallback chain absorbed (local Eastern time).
INCIDENTS = [
    {"date": "2026-10-06", "provider": "Anthropic", "deployment": "anthropic/claude-haiku-4-5", "start": "14:05",
     "minutes": 12, "error": "529 overloaded"},
    {"date": "2026-09-16", "provider": "OpenAI", "deployment": "openai/gpt-4.1-mini", "start": "10:12",
     "minutes": 47, "error": "503 service unavailable"},
    {"date": "2026-07-29", "provider": "Google Gemini", "deployment": "gemini/gemini-2.5-flash", "start": "11:20",
     "minutes": 12, "error": "429 rate limited"},
]  # fmt: skip


def cost(deployment: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES[deployment]
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


def primary_alias(app: str) -> str:
    """The alias an app calls for most of its requests."""
    return max(APPS[app]["routes"], key=lambda r: r[1])[0]


def hour_weights(app: str) -> tuple:
    return HOURS[APPS[app]["hours"]]


def active_hours(app: str) -> range:
    """The hours an app mostly runs in (weight of at least half its peak), as a contiguous range."""
    w = hour_weights(app)
    hours = [h for h in range(24) if w[h] >= 0.5 * max(w)]
    return range(hours[0], hours[-1] + 1)


def apps_of(team: str) -> list[str]:
    return [a for a, spec in APPS.items() if spec["team"] == team]


def first_choice_shares(alias: str, targets: list) -> list[float]:
    """Share of an alias's requests that try each target first: ordered routes always start with the first."""
    mix = LATENCY_MIX.get(alias)
    if mix:
        return list(mix)
    return [1.0] + [0.0] * (len(targets) - 1)

# Corey Mathie, 2026
"""
Write evals/sample_cases.jsonl: 40 route-evaluation cases drawn from the work of the sample company,
Cypress Harbor Credit Union (fictional), with prompts the size its applications send.

Each case is one of the credit union's tasks, tagged with the application that does it (the tags
match demo/cypress_harbor.py): member and online-banking questions answered from the knowledge base,
agent-assist policy checks, card-dispute tagging and Regulation E deadlines, loan-document extraction,
BSA case review, fraud-alert narratives, regulatory-change digests, code review and marketing copy
checks. Contexts (knowledge base, procedures, loan documents, transaction histories) are generated
here from fixed text and a fixed seed, so prompt sizes are realistic and the file is reproducible.
Every name, number, account, routing number and document is invented.

    python scripts/build_eval_cases.py            # write the file
    python scripts/build_eval_cases.py --check    # exit 1 if the committed file differs
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals" / "sample_cases.jsonl"
SEED = 20261007

ROUTING = "266231609"  # fails the ABA checksum on purpose: no real institution has it

KB = [
    (
        "Routing number",
        f"Cypress Harbor Credit Union's routing (ABA) number is {ROUTING}. Use it with your full "
        "account number for direct deposit, ACH transfers and incoming domestic wires.",
    ),
    (
        "Branch hours",
        "Branches are open Monday to Friday, 9:00 am to 5:00 pm, and Saturday, 9:00 am to 1:00 pm. "
        "Branches are closed on Sundays and federal holidays. Drive-up lanes open 30 minutes earlier on weekdays.",
    ),
    (
        "Contact center",
        "Member services answers calls at 954-555-0100 from 7:00 am to 9:00 pm, seven days a week. "
        "Lost or stolen cards can be reported 24 hours a day at the same number, option 1.",
    ),
    (
        "Card lock",
        "To lock a debit or credit card in online or mobile banking, go to Cards > Lock card and choose "
        "the card. A locked card declines new purchases and ATM withdrawals; recurring payments you approved "
        "still post. "
        "Unlock it from the same screen at any time.",
    ),
    (
        "Mobile deposit",
        "Mobile check deposit is available in the mobile app under Deposit > Deposit a check. The "
        "daily limit is $5,000 and the 30-day limit is $15,000. Deposits made before 6:00 pm Eastern on a business day "
        "are usually available the next business day; the first $225 is available the same day.",
    ),
    (
        "Zelle",
        "Members can send up to $1,000 per day and $5,000 per 30 days with Zelle from online or mobile banking. "
        "Zelle payments to someone you do not know cannot be reversed; only send money to people you trust.",
    ),
    (
        "Statements",
        "Statements are available in online banking under Documents > Statements for the last 24 months "
        "and can be downloaded as PDF. Paper statements cost $2.00 per month unless the member is 65 or older or a "
        "student under 24.",
    ),
    (
        "Overdraft",
        "The overdraft fee is $25.00 per item, with a maximum of three overdraft fees per day. No fee is "
        "charged if the account is overdrawn by $10.00 or less at the end of the day. Members can opt out of overdraft "
        "coverage for everyday debit card purchases at any time.",
    ),
    (
        "Overdraft fee waivers",
        "Contact-center agents may waive one overdraft fee per member in any rolling 12-month "
        "period. A second waiver within 12 months needs a supervisor's approval and a note in the member's record.",
    ),
    (
        "Foreign transactions",
        "The Harbor Rewards Visa charges no foreign transaction fee. The Harbor Platinum "
        "Mastercard and debit cards charge 1% of each transaction made in a foreign currency or processed abroad.",
    ),
    (
        "Direct deposit",
        "To set up direct deposit, give your employer the routing number and your 14-digit account "
        "number, or download a pre-filled direct deposit form under Accounts > Direct deposit form.",
    ),
    (
        "Two-step verification",
        "Online banking asks for a one-time code by text or authenticator app at sign-in. To "
        "change the method, go to Settings > Security > Two-step verification. Codes expire after 10 minutes.",
    ),
    (
        "Password reset",
        "Members reset an online banking password from the sign-in page with Forgot password. The "
        "new password must have at least 12 characters. After five failed attempts the account locks for 30 minutes.",
    ),
    (
        "Debit card replacement",
        "A replacement debit card arrives in 7 to 10 business days. Expedited delivery in two "
        "business days costs $25.00. Instant-issue cards are printed at the Fort Lauderdale, Davie and Boca Raton "
        "branches.",
    ),
    (
        "Wire transfers",
        "Outgoing domestic wires cost $20.00 and international wires $45.00. Wires requested in a "
        "branch or by phone before 3:00 pm Eastern are sent the same business day. Incoming domestic wires are free.",
    ),
    (
        "Savings",
        "Primary Savings needs a $5.00 minimum balance, which represents the member's share in the credit "
        "union. Dividends are paid monthly. There is no monthly fee.",
    ),
    (
        "Checking",
        "Basic Checking has no monthly fee. Premier Checking costs $8.00 per month unless the member has "
        "direct deposits of $1,000 or more per month; it includes free checks and refunds up to $15 of other banks' "
        "ATM fees per month.",
    ),
    (
        "Certificates",
        "Share certificates (CDs) need a $500 minimum deposit. An early withdrawal costs 90 days of "
        "dividends for terms of 12 months or less and 180 days for longer terms. Certificates renew automatically at "
        "maturity unless the member asks otherwise within a 10-day grace period.",
    ),
    (
        "Address changes",
        "Members change a mailing address in online banking under Profile > Contact details, or at "
        "any branch with a photo ID. A confirmation letter is mailed to the old and the new address.",
    ),
    (
        "Joint owners",
        "Adding a joint owner requires both owners to sign at a branch with photo ID. Joint owners have "
        "equal access to the account, including statements and online banking.",
    ),
    (
        "Beneficiaries",
        "Payable-on-death beneficiaries can be added to savings, checking and certificates at a "
        "branch. IRAs use a separate beneficiary form.",
    ),
    (
        "ATMs",
        "Members use Cypress Harbor ATMs and more than 30,000 CO-OP network ATMs without a fee. Out-of-network "
        "ATMs cost $2.50 per withdrawal from the credit union, plus any fee the ATM owner charges.",
    ),
    (
        "Loan payments",
        "Loan payments can be made in online banking, by automatic transfer, by phone or at any branch. "
        "Payments received after 5:00 pm Eastern post the next business day. A late fee of $20.00 applies after a "
        "10-day grace period.",
    ),
    (
        "Skip-a-pay",
        "Members with a consumer loan in good standing may skip one payment per calendar year for a $35.00 "
        "fee. Mortgages and credit cards are not eligible. Interest continues to accrue during the skipped month.",
    ),
    (
        "Account closing",
        "Members can close an account at a branch or by signed letter. Pending transactions must "
        "clear first, and the $5.00 share in Primary Savings is returned when membership ends.",
    ),
]


AGENT = """

# Agent assist
You assist contact-center agents during live calls. The agent reads your answer to decide what to tell the member;
quote the policy that applies and keep it short. The member's identity has been verified by the agent.

# Call transcript so far (speech-to-text)
Agent: Thank you for calling Cypress Harbor Credit Union, this is Renee. How can I help you today?
Member: Hi, I'm calling about my checking account. I saw a fee on there and I wanted to see what I can do about it.
Agent: I can look into that for you. Can you confirm the last four of your account number and your date of birth?
Member: Sure, it's 0418, and my birthday is in May. Do you need the full date?
Agent: Yes please, and the zip code on file.
Member: May 12th, and 33024.
Agent: Thank you, you're verified. I see the account. Which fee are you asking about?
Member: There's a twenty-five dollar fee from last week. I think my paycheck came in a day late and a payment went
out before it. It never happens, I'm usually really careful with the account.
Agent: I understand. Let me check what I can do. Can you hold for a moment while I look at the history?
Member: Of course. Also, while you're there, my debit card has been acting up, the chip doesn't read anymore at the
grocery store and I have to swipe it. Can I get a new one quickly? I'm traveling on Saturday.
Agent: I can help with both. Give me one second to pull up the card options and the fee policy.
Member: Thanks. And if you can, tell me what the travel notice thing is, my sister said I should set one up."""

ONLINE = """

# Online and mobile banking chat
You answer in the chat panel of online and mobile banking. The member is signed in; you can see which screen they
are on but not their balances. Menu paths use the names on screen: Accounts, Transfers, Cards, Deposit, Documents,
Profile and Settings. For anything about a specific transaction, offer to start a secure message to member services.
Screens in the mobile app: Accounts (balances, history, direct deposit form), Transfers (between accounts, Zelle,
loan payments), Cards (lock card, travel notice, replace card, change PIN), Deposit (deposit a check, deposit
history), Documents (statements, tax forms, notices), Profile (contact details, alerts), Settings (security,
two-step verification, language, theme)."""


def kb_text(entries: list[tuple[str, str]] | None = None) -> str:
    rows = entries or KB
    body = "\n\n".join(f"## {t}\n{x}" for t, x in rows)
    return (
        "You are the Cypress Harbor Credit Union member assistant (fictional institution). Answer only from the "
        "knowledge base below. If the answer is not in it, say you will connect the member with member services. "
        "Never ask for a full card number, PIN, password or one-time code.\n\n# Knowledge base (excerpt)\n\n" + body
    )


REG_E = """You triage card disputes for Cypress Harbor Credit Union (fictional). Follow the procedure excerpt.

# Card and electronic fund transfer disputes: procedure excerpt (Regulation E)

1. Scope. This procedure covers unauthorized electronic fund transfers and errors on consumer accounts: debit card
purchases, ATM withdrawals, ACH debits, person-to-person payments and online transfers. Credit card disputes follow
the Regulation Z procedure and are out of scope here.

2. Member notice. A member must tell us about an error within 60 calendar days after we sent the first periodic
statement on which the error appeared. Accept notice by phone, in a branch, in secure message or by letter. Record
the date we received the notice; all deadlines below run from that date.

3. Investigation. Complete the investigation within 10 business days of receiving the notice. If it cannot be
completed in 10 business days, give provisional credit for the disputed amount within 10 business days and complete
the investigation within 45 calendar days. Use 90 calendar days instead of 45 for transfers that were initiated
outside the United States, were point-of-sale debit card transactions, or occurred within 30 days after the first
deposit to a new account. For a new account (first deposit within the last 30 days), the provisional-credit
deadline is 20 business days instead of 10. Business days are Monday to Friday, excluding federal holidays.

4. Classification. Tag every dispute with exactly one category:
- fraud: the member did not authorize the transaction and did not benefit from it (stolen card data, account takeover);
- merchant error: the member authorized a purchase but the merchant charged the wrong amount, did not deliver, or did
not process a cancellation or refund;
- duplicate: the same transaction posted more than once;
- member error: the member or someone the member gave the card to authorized the transaction, or the member
mistook a legitimate charge for an unknown one.

5. Results. Tell the member the results within 3 business days after completing the investigation. If we find no
error after giving provisional credit, tell the member the date and amount of the reversal and honor checks and
debits for 5 business days after that notice.

6. Records. Keep the notice, the evidence, the classification and every letter sent for at least two years."""


def dispute_file(rng: random.Random, narrative: str) -> str:
    merchants = [
        "PUBLIX #1132",
        "SHELL OIL 57441",
        "AMAZON MKTPLACE",
        "FPL ONLINE PAYMENT",
        "WAWA 8812",
        "NETFLIX.COM",
        "CVS/PHARMACY 04411",
        "UBER TRIP",
        "TARGET T-1903",
        "COSTCO WHSE #0181",
        "STARBUCKS 21877",
        "AT&T PAYMENT",
    ]
    day = date(2026, 7, 1)
    lines = []
    for _ in range(130):
        day += timedelta(days=rng.choice([0, 0, 0, 1, 1]))
        m = rng.choice(merchants)
        lines.append(f"{day.isoformat()}  POS DEBIT  {m:<22} {rng.uniform(4, 180):>9.2f}  card ending 4417")
    return (
        REG_E
        + "\n\n# Dispute file\n\nMember since 2019. Checking account, debit card ending 4417.\n\nCall notes:\n"
        + narrative
        + "\n\nRecent card activity (most recent last):\n"
        + "\n".join(lines)
    )


BSA = """You help BSA analysts at Cypress Harbor Credit Union (fictional) write case notes. Follow the procedure.

# BSA/AML case review: procedure excerpt

1. Currency transaction reports. File a CTR for cash transactions by or for one person that total more than
$10,000 in one business day, aggregating deposits and withdrawals across branches and accounts.

2. Structuring. Structuring is breaking cash transactions into amounts at or below the CTR threshold to avoid a
report. Indicators: repeated cash deposits just under $10,000 on consecutive days or at different branches, deposits
followed by immediate wires or withdrawals, and a member asking how to avoid paperwork. Structuring is suspicious
whether or not the funds come from a legal source.

3. Suspicious activity reports. When the review supports suspicion, file a SAR within 30 calendar days after the
date the activity was first detected. If no suspect is identified, the deadline may extend by another 30 days, to a
maximum of 60 days. Do not tell the member or anyone involved that a SAR is being considered or has been filed.

4. Case notes. Each note states the typology, the date range, the total amount, the accounts and branches involved,
the decision (file or close) and the reason. Notes stay on-prem: the case-notes app runs only on the on-prem model.

5. Typologies used in case notes: structuring, funnel account, elder financial exploitation, check kiting, mule
activity, and unusual wire activity."""


def bsa_case(rng: random.Random, cash: list[int], extra_note: str) -> str:
    branches = ["Plantation", "Sunrise", "Davie", "Weston", "Pembroke Pines"]
    day = date(2026, 9, 14)
    lines = []
    for amount in cash:
        lines.append(
            f"{day.isoformat()}  CASH DEPOSIT  {rng.choice(branches):<15} ${amount:,.2f}  "
            f"teller {rng.randrange(10, 60)}"
        )
        day += timedelta(days=1)
    other = []
    d2 = date(2026, 8, 1)
    for _ in range(230):
        d2 += timedelta(days=rng.choice([0, 0, 0, 1]))
        other.append(
            f"{d2.isoformat()}  {rng.choice(['POS DEBIT', 'ACH CREDIT', 'ACH DEBIT', 'ATM WITHDRAWAL'])}  "
            f"${rng.uniform(15, 900):,.2f}"
        )
    return (
        BSA
        + "\n\n# Alert 26-0917-044 (monitoring system)\n\nMember: small-business owner, member since 2021, personal "
        "checking and savings.\nRule: cash deposits near the CTR threshold.\n\nFlagged cash activity:\n"
        + "\n".join(lines)
        + f"\n\nAnalyst note: {extra_note}\n\nOther activity, August to September:\n"
        + "\n".join(other)
    )


FRAUD_HEADER = """You write fraud-alert narratives for the risk analytics team at Cypress Harbor Credit Union
(fictional). The card fraud system flagged the alert below overnight. Read the alert and the 90-day card history and
answer the question. Patterns: card testing (small authorizations, often $1.00 or less, at one online merchant,
followed by a large purchase), account takeover (new device, changed contact details, then transfers), lost or stolen
card (card-present use far from the member's usual area), and first-party misuse (the member's own spending
disputed)."""


def fraud_alert(rng: random.Random, alert: list[str]) -> str:
    merchants = [
        "PUBLIX #1132",
        "SHELL OIL 57441",
        "WINN-DIXIE #0244",
        "CHEWY.COM",
        "FPL ONLINE PAYMENT",
        "DUNKIN #34018",
        "HOME DEPOT 6311",
        "SPOTIFY USA",
        "WALGREENS #12203",
        "CHIPOTLE 2104",
        "PLANTATION CAR WASH",
        "BROWARD COUNTY PARKS",
    ]
    day = date(2026, 6, 1)
    lines = []
    for _ in range(250):
        day += timedelta(days=rng.choice([0, 0, 1, 1, 1, 2]))
        lines.append(
            f"{day.isoformat()}  {rng.choice(['CARD PRESENT', 'CARD PRESENT', 'ONLINE']):<12} "
            f"{rng.choice(merchants):<22} FL  {rng.uniform(3, 160):>8.2f}  approved"
        )
    return (
        FRAUD_HEADER
        + "\n\n# Alert\n\n"
        + "\n".join(alert)
        + "\n\n# Card history since June 1, debit card ending 2209 (oldest first)\n\n"
        + "\n".join(lines)
    )


def supporting_docs(rng: random.Random) -> str:
    """The application packet that comes with the contract: pay stubs and a bank statement, OCR text."""
    stubs = []
    day = date(2026, 1, 2)
    for _ in range(20):
        gross = 2884.62
        stubs.append(
            f"{day.isoformat()}  Harbor Logistics LLC  gross {gross:,.2f}  federal {gross * 0.11:,.2f}  "
            f"FICA {gross * 0.0765:,.2f}  net {gross * 0.8135:,.2f}"
        )
        day += timedelta(days=14)
    deposits = []
    day = date(2025, 10, 3)
    for _ in range(60):
        deposits.append(
            f"{day.isoformat()}  {rng.choice(['PAYROLL HARBOR LOGISTICS', 'TRANSFER FROM SAVINGS', 'MOBILE DEPOSIT'])}"
            f"  {rng.uniform(80, 2400):>9.2f}  balance {rng.uniform(900, 7400):>9.2f}"
        )
        day += timedelta(days=6)
    return (
        "\n\n# Attached: pay stubs, borrower (OCR)\n"
        + "\n".join(stubs)
        + "\n\n# Attached: deposit history, other institution, last 12 months (OCR)\n"
        + "\n".join(deposits)
        + "\n\n# Attached: credit application summary\nEmployer: Harbor Logistics LLC (fictional), dispatcher, "
        "6 years. "
        "Housing: rent $1,650 per month. Other debts: student loan $212 per month, credit card minimums $85 per month. "
        "Co-borrower employer: Broward County Schools (fictional entry), teacher's aide, 3 years, gross $2,410 per "
        "month."
    )


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


LOAN = {
    "amount": 28450.00, "apr": 6.24, "months": 72, "first": date(2026, 11, 15), "contract": "AL-2026-118734",
    "branch": "Coral Springs", "officer": 412, "borrower": "Marisol Vega", "co": "Dana Ortiz",
    "address": "1450 NW 99th Ave, Coral Springs, FL 33071", "vehicle": "2024 Harbor Motors Coastline SE",
    "vin": "1HM0FICT1RA012345", "odometer": "18,214", "cash_price": "31,950.00", "down": "4,500.00",
    "doc_fee": "999.00", "gap": "GAP coverage declined.", "signed": "2026-10-02",
}  # fmt: skip


def loan_doc(rng: random.Random, p: dict | None = None) -> tuple[str, dict]:
    p = {**LOAN, **(p or {})}
    amount, apr, months = p["amount"], p["apr"], p["months"]
    r = apr / 100 / 12
    payment = round(amount * r / (1 - (1 + r) ** -months), 2)
    first = p["first"]
    schedule = []
    balances = []
    bal = amount
    for i in range(months):
        interest = round(bal * r, 2)
        principal = round(payment - interest, 2)
        bal = round(bal - principal, 2)
        balances.append(max(bal, 0))
        due = date(first.year + (first.month - 1 + i) // 12, (first.month - 1 + i) % 12 + 1, first.day)
        schedule.append(
            f"{i + 1:>3}  {due.isoformat()}  {payment:>8.2f}  {interest:>7.2f}  {principal:>8.2f}  {max(bal, 0):>10.2f}"
        )
    begin, nth = f"{first:%B} {first.day}, {first.year}", ordinal(first.day)
    items = f"cash price ${p['cash_price']}; down payment ${p['down']}; documentary fee ${p['doc_fee']}"
    text = (
        f"""You extract fields from loan documents for Cypress Harbor Credit Union's lending team (fictional). Answer
with the value only, as it appears in the document unless the question asks for a format.

# Document: Motor Vehicle Retail Installment Contract and Security Agreement (scanned, OCR text)

Contract number: {p["contract"]}        Branch: {p["branch"]}        Loan officer: staff {p["officer"]}
Borrower: {p["borrower"]:<20}          Co-borrower: {p["co"]}
Address: {p["address"]} (fictional)

FEDERAL TRUTH-IN-LENDING DISCLOSURES
ANNUAL PERCENTAGE RATE: {apr:.2f}%      FINANCE CHARGE: ${payment * months - amount:,.2f}
Amount Financed: ${amount:,.2f}         Total of Payments: ${payment * months:,.2f}
Payment schedule: {months} monthly payments of ${payment:,.2f} beginning {begin}, and on the {nth} day of
each month after that.

Collateral: {p["vehicle"]}, VIN {p["vin"]}, odometer {p["odometer"]} miles.
Itemization of amount financed: {items} (paid in
cash); title and registration $0.00 financed; {p["gap"]}

Late charge: if a payment is more than 10 days late, a late charge of $20.00 may be assessed.
Prepayment: if you pay off early, you will not have to pay a penalty.
Security interest: you are giving a security interest in the vehicle being purchased.
Insurance: you must keep the vehicle insured against physical damage, naming Cypress Harbor Credit Union as loss
payee. If you do not, we may buy insurance and add its cost to the loan.

Governing law: Florida. Arbitration: either party may elect arbitration of any claim, except small-claims matters.
Signatures: {p["borrower"]} {p["signed"]}; {p["co"]} {p["signed"]}.

AMORTIZATION SCHEDULE (no.  due date  payment  interest  principal  balance)
"""
        + "\n".join(schedule)
        + supporting_docs(rng)
    )
    return text, {
        "amount": f"${amount:,.2f}",
        "apr": f"{apr:.2f}%",
        "months": str(months),
        "payment": f"${payment:,.2f}",
        "first": first.isoformat(),
        "co": p["co"],
        "vin": p["vin"],
        "finance": f"${payment * months - amount:,.2f}",
        "total": f"${payment * months:,.2f}",
        "balance_12": f"{balances[11]:,.2f}",
    }


REG_NOTICE = """You write regulatory-change digests for the compliance team at Cypress Harbor Credit Union
(fictional). Read the notice and answer the question in a few words.

# FICTIONAL NOTICE FOR EVALUATION ONLY (not a real rulemaking)

AGENCY: Consumer Financial Protection Bureau (fictional notice).
ACTION: Proposed rule; request for comment.
SUMMARY: The Bureau proposes to amend Regulation E (12 CFR part 1005) to require financial institutions to give
consumers a written notice, within 2 business days, when a person-to-person payment that the consumer reports as
fraudulent was sent to an account the institution has previously flagged for fraud. The proposal would also require
institutions to keep records of such reports for three years.
DATES: Comments must be received on or before December 14, 2026. The Bureau proposes an effective date of July 1, 2027.
"""


def reg_notice(rng: random.Random, header: str = REG_NOTICE, section: str = "1005", topics: list | None = None) -> str:
    topics = topics or [
        "the definition of person-to-person payment",
        "flagged-account notices",
        "record retention",
        "small-entity impact",
        "coordination with Regulation Z",
        "error-resolution timing",
        "consumer notice content",
        "payment network rules",
        "credit union implementation costs",
        "data security of flag lists",
    ]
    paras = []
    for i in range(1, 49):
        t = rng.choice(topics)
        paras.append(
            f"Section-by-section analysis, comment {i}: Several commenters to the advance notice addressed "
            f"{t}. The Bureau considered whether the proposal should apply to institutions of every size and "
            "preliminarily concludes that the notice requirement in proposed section "
            f"{section}.{rng.randrange(11, 35)} "
            "should apply uniformly, while seeking comment on alternatives for institutions with fewer than "
            f"{rng.choice([50, 100, 250])} employees and on the cost of compliance for each alternative."
        )
    return header + "\nSUPPLEMENTARY INFORMATION:\n\n" + "\n\n".join(paras)


CODE = '''You review code for the IT engineering team at Cypress Harbor Credit Union (fictional). Answer the question
about the module below in a few words.

# ledger/fees.py

from decimal import ROUND_HALF_UP, Decimal

OVERDRAFT_FEE = Decimal("25.00")
MAX_OVERDRAFT_FEES_PER_DAY = 3
DE_MINIMIS = Decimal("10.00")


class FeeError(Exception):
    """Raised when a fee cannot be assessed."""


def round_cents(amount: Decimal) -> Decimal:
    """Round a dollar amount to cents, half up."""
    return amount.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def overdraft_fees(end_of_day_balance: Decimal, items_paid: int) -> Decimal:
    """Overdraft fees for one day: none at or above -$10.00, at most three items."""
    if end_of_day_balance >= -DE_MINIMIS:
        return Decimal("0.00")
    return round_cents(OVERDRAFT_FEE * min(items_paid, MAX_OVERDRAFT_FEES_PER_DAY))


def paper_statement_fee(age: int, is_student: bool) -> Decimal:
    """$2.00 a month, waived for members 65 or older and students under 24."""
    if age >= 65 or (is_student and age < 24):
        return Decimal("0.00")
    return Decimal("2.00")


def gateway_status(error: str) -> int:
    """HTTP status the AI gateway returns for each refusal, as the client SDK maps them."""
    return {
        "budget_exceeded": 402,
        "policy_denied": 403,
        "pii_blocked": 422,
        "rate_limited": 429,
        "all_circuits_open": 503,
    }[error]
'''


def code_context(rng: random.Random) -> str:
    tests = []
    for i in range(1, 81):
        bal = rng.choice([-5, -9.99, -10, -10.01, -50, -250, 0, 120])
        items = rng.randrange(0, 6)
        tests.append(
            f'def test_overdraft_case_{i:02d}():\n    assert overdraft_fees(Decimal("{bal}"), {items}) is not None\n'
        )
    return (
        CODE
        + "\n# tests/test_fees.py (excerpt)\n\nfrom ledger.fees import overdraft_fees\nfrom decimal import Decimal\n\n"
        + "\n".join(tests)
    )


RATES = """You check marketing drafts for Cypress Harbor Credit Union (fictional) against the current rate sheet.
Answer with the value only.

# Rate sheet, effective October 1, 2026 (dividends compounded monthly; APY = annual percentage yield)

| Product | Minimum | Dividend rate | APY |
|---|---|---|---|
| Primary Savings | $5 | 0.25% | 0.25% |
| Holiday Club | $0 | 0.50% | 0.50% |
| Money Market | $2,500 | 3.05% | 3.10% |
| 6-month certificate | $500 | 3.84% | 3.90% |
| 12-month certificate | $500 | 4.07% | 4.15% |
| 18-month certificate | $500 | 3.97% | 4.05% |
| 36-month certificate | $500 | 3.68% | 3.75% |

Rates may change after the account is opened for savings and money market accounts. Certificate rates are fixed for
the term. A penalty may be imposed for early withdrawal from a certificate. Federally insured by NCUA.

# Brand and compliance checklist for rate ads
- Every rate in an ad is an APY, stated with its term and the minimum deposit, and matches the current rate sheet.
- Say "dividends", not "interest", for savings, money market and certificates; "Federally insured by NCUA" appears
  in the footer of every ad that mentions a deposit product.
- No "guaranteed", "risk-free" or "best rate in Florida" claims; no comparisons with named competitors.
- Membership eligibility is stated in full whenever an ad invites people to open an account.
- Spanish and Haitian Creole versions are reviewed by a native speaker before release.
- Headlines in sentence case; numerals for all rates and amounts; the credit union's name in full on first use.
- Images show real branches or stock photography approved in the asset library; no images of currency.
- Digital ads link to the rate sheet page; print ads include the short URL cypressharbor.example/rates.
- Compliance reviews any ad that mentions a rate, a fee, a loan term or eligibility before it is scheduled.

# Draft
"Lock in 4.15% APY for 12 months at Cypress Harbor. Open a certificate with just $500, online or at any of our 11
branches across Broward and Palm Beach counties. Members only; membership is open to anyone who lives, works,
worships or attends school in Broward or Palm Beach County."

Email body (draft 3, for the October member newsletter):
"Saving for something big? Our 12-month certificate now earns 4.15% APY, and it takes just $500 to open. Your rate is
fixed for the whole term, so you know exactly what you will earn. Prefer more flexibility? A Money Market account
earns 3.10% APY on balances of $2,500 or more, and you can move money in and out any time. Not sure which is right for
you? Stop by any of our 11 branches or book a 20-minute savings check-in with a member advisor online. We will look at
your goals, your timeline and how much you want to keep on hand for emergencies, and help you split your savings the
way that works for you. Dividends are compounded monthly. A penalty may be imposed for early withdrawal from a
certificate. Rates are effective October 1, 2026 and may change. Federally insured by NCUA."
"""


def case(cid: str, tag: str, system: str, question: str, ref: tuple[str, str], answer: str, max_tokens: int) -> dict:
    return {
        "id": cid,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
        "reference": {"type": ref[0], "value": ref[1]},
        "answer": answer,
        "tags": [tag],
        "max_tokens": max_tokens,
    }


def build() -> list[dict]:
    rng = random.Random(SEED)
    out: list[dict] = []
    kb = kb_text()
    faq = [
        ("What is the credit union's routing number? Digits only.", ("exact", ROUTING), ROUTING),
        ("Is a branch open at 11:30 am on a Saturday? Answer open or closed.", ("exact", "open"), "open"),
        ("What is the overdraft fee per item, in dollars?", ("regex", r"\$?25(\.00)?\b"), "$25.00"),
        (
            "A member wants to send $1,500 with Zelle today. What is the daily limit, in dollars?",
            ("regex", r"\$?1,?000\b"),
            "$1,000",
        ),
        ("How much of a mobile check deposit is available the same day, in dollars?", ("regex", r"\$?225\b"), "$225"),
        (
            "Does the Harbor Rewards Visa charge a foreign transaction fee? Answer yes or none.",
            ("exact", "none"),
            "none",
        ),
        ("How many months of statements can be downloaded in online banking? Number only.", ("exact", "24"), "24"),
    ]
    for i, (q, ref, ans) in enumerate(faq, 1):
        out.append(case(f"member-{i:02d}", "member-assistant", kb, q, ref, ans, 128))
    online = [
        (
            "Where in the app does a member lock a card? Give the menu path.",
            ("contains", "Cards > Lock card"),
            "Cards > Lock card",
        ),
        (
            "How long until a locked-out member can try signing in again, in minutes? Number only.",
            ("exact", "30"),
            "30",
        ),
        ("What is the minimum password length in characters? Number only.", ("exact", "12"), "12"),
    ]
    for i, (q, ref, ans) in enumerate(online, 1):
        out.append(case(f"online-{i:02d}", "online-banking", kb + ONLINE, q, ref, ans, 128))
    agent = [
        (
            "Call notes: member had an overdraft fee waived on 2026-03-02 and asks for another waiver today, "
            "2026-10-07. "
            "Can the agent waive it without a supervisor? Answer eligible or ineligible.",
            ("exact", "ineligible"),
            "ineligible",
        ),
        (
            "Call notes: member lost their debit card and needs one by Friday; today is Wednesday. What does expedited "
            "delivery cost, in dollars?",
            ("regex", r"\$?25(\.00)?\b"),
            "$25.00",
        ),
    ]
    for i, (q, ref, ans) in enumerate(agent, 1):
        out.append(case(f"agent-{i:02d}", "agent-assist", kb + AGENT, q, ref, ans, 128))

    disputes = [
        (
            "2026-08-03: Member reports a $64.12 charge at SHELL OIL 57441 in Tampa on 2026-07-29. The member was in "
            "Fort Lauderdale all week and still has the card. No one else uses it.",
            "fraud",
        ),
        (
            "2026-08-03: Member cancelled a gym membership in June (has the cancellation email) but was charged $49.99 "
            "again on 2026-07-28 by the gym.",
            "merchant error",
        ),
        (
            "2026-08-03: Member sees two identical $87.40 charges at TARGET T-1903 on 2026-07-30, one purchase made.",
            "duplicate",
        ),
        (
            "2026-08-03: Member did not recognize 'SQ *HARBOR BREW' for $14.50; after checking, it is the coffee shop "
            "next to the member's office, where the member paid with this card.",
            "member error",
        ),
    ]
    for i, (narr, tag) in enumerate(disputes, 1):
        out.append(
            case(
                f"dispute-{i:02d}",
                "dispute-triage",
                dispute_file(rng, narr),
                "Tag this dispute: fraud, merchant error, duplicate or member error. Category only.",
                ("exact", tag),
                tag,
                64,
            )
        )
    deadlines = [
        (
            "The member reported the $64.12 SHELL OIL charge on Monday 2026-08-03 and the account is six years old. If "
            "the investigation is not finished, by what date must provisional credit be given? YYYY-MM-DD.",
            "2026-08-17",
        ),
        (
            "Same dispute, reported on 2026-08-03, provisional credit given. By what date must the investigation be "
            "complete? It is a point-of-sale debit card transaction. YYYY-MM-DD.",
            "2026-11-01",
        ),
        (
            "The error first appeared on the statement sent on 2026-07-31. What is the last day the member can notify "
            "the credit union under the procedure? YYYY-MM-DD.",
            "2026-09-29",
        ),
    ]
    for i, (q, ans) in enumerate(deadlines, 1):
        out.append(
            case(
                f"rege-{i:02d}",
                "dispute-triage",
                dispute_file(rng, disputes[0][0]),
                q,
                ("regex", rf"\b{ans}\b"),
                ans,
                64,
            )
        )

    doc, f = loan_doc(rng)
    loans = [
        ("What is the amount financed?", ("contains", f["amount"]), f["amount"]),
        ("What is the annual percentage rate?", ("contains", f["apr"]), f["apr"]),
        ("How many monthly payments are there? Number only.", ("exact", f["months"]), f["months"]),
        ("What is the monthly payment?", ("contains", f["payment"]), f["payment"]),
        ("When is the first payment due? YYYY-MM-DD.", ("regex", rf"\b{f['first']}\b"), f["first"]),
    ]
    for i, (q, ref, ans) in enumerate(loans, 1):
        out.append(case(f"loan-{i:02d}", "loan-doc-extraction", doc, q, ref, ans, 64))

    cash = [9500, 9700, 9800, 9400]
    case_text = bsa_case(
        rng, cash, "Member asked teller 31 on 2026-09-16 whether deposits under $10,000 'avoid the form'."
    )
    bsa = [
        ("Which typology fits the flagged cash activity? One word.", ("exact", "structuring"), "structuring"),
        (
            "What is the total of the flagged cash deposits, in dollars?",
            ("regex", rf"\$?{sum(cash):,}(\.00)?\b".replace(",", ",?")),
            f"${sum(cash):,.2f}",
        ),
        (
            "Activity was first detected on 2026-09-17. By what date must a SAR be filed if a suspect is identified? "
            "YYYY-MM-DD.",
            ("regex", r"\b2026-10-17\b"),
            "2026-10-17",
        ),
        ("Above what one-day cash total must a CTR be filed, in dollars?", ("regex", r"\$?10,?000\b"), "$10,000"),
    ]
    for i, (q, ref, ans) in enumerate(bsa, 1):
        out.append(case(f"bsa-{i:02d}", "bsa-case-notes", case_text, q, ref, ans, 128))

    alert = [
        "Alert FA-2026-10-0418, debit card ending 2209, score 912/999.",
        "2026-10-06 02:11  ONLINE  QUICKPAY*DONATE    US  1.00  approved",
        "2026-10-06 02:12  ONLINE  QUICKPAY*DONATE    US  1.00  approved",
        "2026-10-06 02:12  ONLINE  QUICKPAY*DONATE    US  1.00  approved",
        "2026-10-06 02:19  ONLINE  ELECTRO HUB OUTLET US  849.99  declined by rule CNP-HIGH",
    ]
    ctx = fraud_alert(rng, alert)
    fraud = [
        (
            "Which pattern does the alert show? Answer with the pattern name.",
            ("contains", "card testing"),
            "card testing",
        ),
        ("How many $1.00 test authorizations preceded the large purchase? Number only.", ("exact", "3"), "3"),
        ("What was the amount of the declined purchase, in dollars?", ("regex", r"\$?849\.99\b"), "$849.99"),
        ("At which merchant was the large purchase attempted?", ("contains", "ELECTRO HUB"), "ELECTRO HUB OUTLET"),
        ("Was the large purchase card present or online? One word.", ("exact", "online"), "online"),
    ]
    for i, (q, ref, ans) in enumerate(fraud, 1):
        out.append(case(f"fraud-{i:02d}", "fraud-alert-narratives", ctx, q, ref, ans, 256))

    notice = reg_notice(rng)
    reg = [
        ("Which regulation would the proposal amend?", ("contains", "Regulation E"), "Regulation E"),
        ("When are comments due? YYYY-MM-DD.", ("regex", r"\b2026-12-14\b"), "2026-12-14"),
        ("Within how many business days would the notice be due? Number only.", ("exact", "2"), "2"),
    ]
    for i, (q, ref, ans) in enumerate(reg, 1):
        out.append(case(f"reg-{i:02d}", "reg-change-digest", notice, q, ref, ans, 128))

    code = code_context(rng)
    review = [
        (
            "Which function rounds to the wrong number of decimal places? Function name only.",
            ("exact", "round_cents"),
            "round_cents",
        ),
        (
            "What HTTP status does gateway_status return when a budget is exceeded? Number only.",
            ("exact", "402"),
            "402",
        ),
    ]
    for i, (q, ref, ans) in enumerate(review, 1):
        out.append(case(f"code-{i:02d}", "code-assistant", code, q, ref, ans, 64))

    marketing = [
        (
            "Does the APY in the draft match the rate sheet for the 12-month certificate? Answer matches or differs.",
            ("exact", "matches"),
            "matches",
        ),
        (
            "Which product on the rate sheet has the highest APY? Product name only.",
            ("contains", "12-month certificate"),
            "12-month certificate",
        ),
    ]
    for i, (q, ref, ans) in enumerate(marketing, 1):
        out.append(case(f"marketing-{i:02d}", "content-drafts", RATES, q, ref, ans, 64))
    return out + more(random.Random(SEED + 1))


# ---------------------------------------------------------------------------------------------
# The larger set: 224 more cases, from a second seed so the first 40 cases above stay byte-identical.
# Every app gets cases, including mobile-banking. Cases marked HARD carry a second tag, "hard": boundary
# values (exactly $10,000, exactly -$10.00), deadlines across federal holidays, lookalike figures (a
# certificate's dividend rate against its APY), questions the context cannot answer, and code whose
# behaviour differs from its docstring. In the simulation every case is scored the same way, so "hard"
# only matters when real models are evaluated (--provider real); see docs/evaluation.md.
# ---------------------------------------------------------------------------------------------

HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3),
    date(2026, 9, 7), date(2026, 10, 12), date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25),
}  # fmt: skip
HARD = "hard"


def business_days_after(start: date, n: int) -> date:
    """The nth business day after `start` (Monday to Friday, excluding the federal holidays above)."""
    d = start
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in HOLIDAYS_2026:
            n -= 1
    return d


MOBILE = """

# Mobile app chat
You answer in the chat panel of the Cypress Harbor mobile app for iOS and Android. The member is signed in; you can
see which screen they are on but not their balances. Use the screen names in the app. For anything about a specific
transaction, offer to start a secure message to member services.
App facts:
- Biometric sign-in (Face ID, Touch ID or fingerprint) is under Settings > Security > Biometric sign-in. After 3
failed biometric attempts the app asks for the password instead.
- The app supports iOS 16 or later and Android 11 or later.
- Mobile deposit: endorse the back "For mobile deposit only at Cypress Harbor CU" with your signature, photograph the
front and back on a dark surface, keep the check for 14 days, then destroy it. Checks payable to someone else,
foreign checks and savings bonds cannot be deposited in the app.
- Alerts are under Profile > Alerts. Low-balance alerts can be set from $25 to $5,000.
- Card controls (Cards > Card controls) block online purchases, international purchases or ATM withdrawals
separately, without locking the card.
- Travel notices (Cards > Travel notice) can be set up to 90 days ahead and cover up to 5 destinations each.
- Quick balance shows the available balance of up to 2 accounts before sign-in; turn it on in Settings > Quick
balance.
- The app signs the member out after 5 minutes without activity."""


def num(v: str) -> tuple[str, str]:
    """A dollar amount with or without "$", cents and thousands separator: "$1,000" matches 1000 and $1,000.00."""
    whole, _, cents = v.lstrip("$").partition(".")
    pattern = r"\$?" + whole.replace(",", ",?") + (r"(\.00)?" if cents in ("", "00") else r"\." + cents) + r"\b"
    return ("regex", pattern)


def ex(v: str) -> tuple[str, str]:
    return ("exact", v)


def has(v: str) -> tuple[str, str]:
    return ("contains", v)


def day(v: str) -> tuple[str, str]:
    return ("regex", rf"\b{v}\b")


def add(out: list[dict], prefix: str, tag: str, system: str, rows: list, max_tokens: int, start: int) -> None:
    """rows: (question, reference, answer) or (question, reference, answer, HARD)."""
    for i, row in enumerate(rows, start):
        c = case(f"{prefix}-{i:02d}", tag, system, row[0], row[1], row[2], max_tokens)
        if len(row) > 3 and row[3] == HARD:
            c["tags"].append(HARD)
        out.append(c)


def bsa_entries(rng: random.Random, alert: str, profile: str, rule: str, entries: list, note: str) -> str:
    """A BSA alert with explicit (date, branch, amount) cash entries, several a day where the case needs it."""
    lines = [f"{d}  CASH DEPOSIT  {b:<15} ${a:,.2f}  teller {rng.randrange(10, 60)}" for d, b, a in entries]
    other = []
    d2 = date(2026, 8, 1)
    for _ in range(230):
        d2 += timedelta(days=rng.choice([0, 0, 0, 1]))
        other.append(
            f"{d2.isoformat()}  {rng.choice(['POS DEBIT', 'ACH CREDIT', 'ACH DEBIT', 'ATM WITHDRAWAL'])}  "
            f"${rng.uniform(15, 900):,.2f}"
        )
    return (
        BSA
        + f"\n\n# Alert {alert} (monitoring system)\n\nMember: {profile}\nRule: {rule}\n\nFlagged cash activity:\n"
        + "\n".join(lines)
        + f"\n\nAnalyst note: {note}\n\nOther activity, August to September:\n"
        + "\n".join(other)
    )


NOTICES = [
    (
        """You write regulatory-change digests for the compliance team at Cypress Harbor Credit Union
(fictional). Read the notice and answer the question in a few words.

# FICTIONAL NOTICE FOR EVALUATION ONLY (not a real rulemaking)

AGENCY: National Credit Union Administration (fictional notice).
ACTION: Proposed rule; request for comment.
SUMMARY: The Board proposes to amend 12 CFR part 748 to require federally insured credit unions with total assets of
$500 million or more to test their incident response plan at least once every 12 months and to report a
cyber incident that disrupts member access to accounts for more than 4 hours within 24 hours of discovering it. Credit
unions under $500 million would test at least once every 24 months.
DATES: Comments must be received on or before January 19, 2027. The Board proposes an effective date of October 1,
2027.
""",
        "748",
        {
            "agency": "National Credit Union Administration",
            "part": "748",
            "comments": "2027-01-19",
            "effective": "2027-10-01",
            "report_hours": "24",
            "asset_line": "$500 million",
            "test_months_large": "12",
            "test_months_small": "24",
        },
    ),
    (
        """You write regulatory-change digests for the compliance team at Cypress Harbor Credit Union
(fictional). Read the notice and answer the question in a few words.

# FICTIONAL NOTICE FOR EVALUATION ONLY (not a real rulemaking)

AGENCY: Consumer Financial Protection Bureau (fictional notice).
ACTION: Final rule.
SUMMARY: The Bureau amends Regulation Z (12 CFR part 1026) to require card issuers to send an electronic or written
alert at least 7 days before a promotional rate on a credit card account expires, and to state the rate that will
apply afterwards. The rule applies to issuers with more than 10,000 open credit card accounts.
DATES: This rule is effective April 1, 2027. Compliance is mandatory for promotional rates that expire on or after
July 1, 2027.
""",
        "1026",
        {
            "agency": "Consumer Financial Protection Bureau",
            "part": "1026",
            "regulation": "Regulation Z",
            "effective": "2027-04-01",
            "mandatory": "2027-07-01",
            "days": "7",
            "accounts": "10,000",
            "kind": "final rule",
        },
    ),
    (
        """You write regulatory-change digests for the compliance team at Cypress Harbor Credit Union
(fictional). Read the notice and answer the question in a few words.

# FICTIONAL NOTICE FOR EVALUATION ONLY (not a real rulemaking)

AGENCY: Financial Crimes Enforcement Network (fictional notice).
ACTION: Proposed rule; request for comment.
SUMMARY: FinCEN proposes to amend 31 CFR part 1020 to lower the recordkeeping threshold for funds transfers from
$3,000 to $250 for transfers that begin or end outside the United States, and to require institutions to keep those
records for 5 years.
DATES: Comments must be received on or before February 26, 2027.
""",
        "1020",
        {
            "agency": "Financial Crimes Enforcement Network",
            "part": "1020",
            "comments": "2027-02-26",
            "old": "$3,000",
            "new": "$250",
            "years": "5",
        },
    ),
]


def more(rng: random.Random) -> list[dict]:
    out: list[dict] = []
    kb = kb_text()
    unknown = "I will connect you with member services."
    add(out, "member", "member-assistant", kb, [
        ("What does an outgoing domestic wire cost, in dollars?", num("$20.00"), "$20.00"),
        ("What does an international wire cost, in dollars?", num("$45.00"), "$45.00"),
        ("By what time must a wire be requested to go out the same business day? Time only.", has("3:00 pm"),
         "3:00 pm"),
        ("What is the monthly fee for Premier Checking, in dollars?", num("$8.00"), "$8.00"),
        ("How much in monthly direct deposits waives the Premier Checking fee, in dollars?", num("$1,000"), "$1,000"),
        ("What minimum balance does Primary Savings need, in dollars?", num("$5.00"), "$5.00"),
        ("What does the credit union charge per out-of-network ATM withdrawal, in dollars?", num("$2.50"), "$2.50"),
        ("What does skip-a-pay cost, in dollars?", num("$35.00"), "$35.00"),
        ("Can a member use skip-a-pay on a mortgage? Answer yes or no.", ex("no"), "no"),
        ("What is the early-withdrawal penalty on an 18-month certificate?", has("180 days"), "180 days of dividends"),
        ("What is the early-withdrawal penalty on a 12-month certificate?", has("90 days"), "90 days of dividends",
         HARD),
        ("How many days is the grace period after a certificate matures? Number only.", ex("10"), "10"),
        ("How many days is the grace period before a loan late fee applies? Number only.", ex("10"), "10"),
        ("Does a 70-year-old member pay for paper statements? Answer yes or no.", ex("no"), "no"),
        ("What do paper statements cost a 30-year-old member who is not a student, per month?", num("$2.00"), "$2.00"),
        ("What is the dividend rate on a 60-month certificate?", has("member services"), unknown, HARD),
        ("A member sent $900 with Zelle today. Can they send another $200 today? Answer yes or no.", ex("no"), "no",
         HARD),
        ("Can a member deposit a $5,500 check in the app today? Answer yes or no.", ex("no"), "no", HARD),
        ("What foreign transaction fee does a debit card charge, as a percentage?", ("regex", r"\b1(\.0+)?\s?%"), "1%"),
        ("After how many minutes does a sign-in code expire? Number only.", ex("10"), "10"),
    ], 128, 8)  # fmt: skip

    online = kb + ONLINE
    add(out, "online", "online-banking", online, [
        ("Where does a member download a direct deposit form? Menu path.", has("Accounts > Direct deposit form"),
         "Accounts > Direct deposit form"),
        ("Where does a member change the two-step verification method? Menu path.",
         has("Settings > Security > Two-step verification"), "Settings > Security > Two-step verification"),
        ("Where are statements? Menu path.", has("Documents > Statements"), "Documents > Statements"),
        ("Where does a member change their mailing address? Menu path.", has("Profile > Contact details"),
         "Profile > Contact details"),
        ("After how many failed sign-in attempts does the account lock? Number only.", ex("5"), "5"),
        ("Besides text message, how can a member get a sign-in code?", has("authenticator"), "an authenticator app"),
        ("A member asks why a $42.17 charge from yesterday is pending. What should you offer?", has("secure message"),
         "a secure message to member services", HARD),
        ("How many digits does an account number have? Number only.", ex("14"), "14"),
        ("What is the Zelle limit per 30 days, in dollars?", num("$5,000"), "$5,000"),
        ("What is the 30-day mobile deposit limit, in dollars?", num("$15,000"), "$15,000"),
        ("Before what time must a mobile deposit be made for next-business-day availability? Time only.",
         has("6:00 pm"), "6:00 pm"),
        ("Which screen has tax forms? Screen name only.", ex("Documents"), "Documents"),
        ("Which screen has the language setting? Screen name only.", ex("Settings"), "Settings"),
        ("Which screen has the travel notice? Screen name only.", ex("Cards"), "Cards"),
        ("A member wants the statement from 30 months ago. Can they download it in online banking? Answer yes or no.",
         ex("no"), "no", HARD),
        ("Which screen shows balances? Screen name only.", ex("Accounts"), "Accounts"),
        ("A member was locked out after five failed attempts at 2:00 pm. From what time can they try again?",
         has("2:30 pm"), "2:30 pm", HARD),
        ("Is a 10-character password long enough? Answer yes or no.", ex("no"), "no"),
        ("What does an incoming domestic wire cost? Answer free or the fee in dollars.", ex("free"), "free"),
        ("Which screen has loan payments? Screen name only.", ex("Transfers"), "Transfers"),
        ("Can a Zelle payment to a stranger be reversed? Answer yes or no.", ex("no"), "no"),
    ], 128, 4)  # fmt: skip

    mobile = kb + MOBILE
    add(out, "mobile", "mobile-banking", mobile, [
        ("Where does a member turn on Face ID? Menu path.", has("Settings > Security > Biometric sign-in"),
         "Settings > Security > Biometric sign-in"),
        ("After how many failed biometric attempts does the app ask for the password? Number only.", ex("3"), "3"),
        ("What is the oldest iOS version the app supports? Number only.", ex("16"), "16"),
        ("What is the oldest Android version the app supports? Number only.", ex("11"), "11"),
        ("What should the member write on the back of a check for mobile deposit?",
         has("For mobile deposit only at Cypress Harbor CU"), "For mobile deposit only at Cypress Harbor CU"),
        ("How many days should the member keep a deposited check? Number only.", ex("14"), "14"),
        ("Can a member deposit a savings bond in the app? Answer yes or no.", ex("no"), "no"),
        ("Where are alerts set up? Menu path.", has("Profile > Alerts"), "Profile > Alerts"),
        ("What is the lowest amount a low-balance alert can be set to, in dollars?", num("$25"), "$25"),
        ("What is the highest amount a low-balance alert can be set to, in dollars?", num("$5,000"), "$5,000"),
        ("Where does a member block international purchases without locking the card? Menu path.",
         has("Cards > Card controls"), "Cards > Card controls"),
        ("How many days ahead can a travel notice be set? Number only.", ex("90"), "90"),
        ("How many destinations can one travel notice cover? Number only.", ex("5"), "5"),
        ("How many accounts can quick balance show? Number only.", ex("2"), "2"),
        ("After how many minutes without activity does the app sign the member out? Number only.", ex("5"), "5"),
        ("A member's iPhone runs iOS 15. Is it supported? Answer yes or no.", ex("no"), "no", HARD),
        ("Can a member deposit a $6,000 check in the app today? Answer yes or no.", ex("no"), "no", HARD),
        ("Where does a member deposit a check? Menu path.", has("Deposit > Deposit a check"),
         "Deposit > Deposit a check"),
        ("A member's phone runs Android 12. Is it supported? Answer yes or no.", ex("yes"), "yes"),
        ("Does the app work on a smartwatch?", has("member services"), unknown, HARD),
        ("What is the Zelle daily limit, in dollars?", num("$1,000"), "$1,000"),
        ("Where does a member lock a card? Menu path.", has("Cards > Lock card"), "Cards > Lock card"),
        ("Can a member set a travel notice 120 days before a trip? Answer yes or no.", ex("no"), "no", HARD),
        ("Can a member deposit a check made out to their roommate? Answer yes or no.", ex("no"), "no"),
    ], 128, 1)  # fmt: skip

    agent = kb + AGENT
    waivers = [("2025-09-30", "eligible"), ("2025-10-08", "ineligible"), ("2025-10-06", "eligible"),
               ("2026-06-14", "ineligible"), ("2024-12-01", "eligible")]  # fmt: skip
    rows = [
        (
            f"Call notes: the member's last overdraft-fee waiver was on {w}. Today is 2026-10-07. Can the agent waive "
            "today's fee without a supervisor? Answer eligible or ineligible.",
            ex(ans),
            ans,
            *([HARD] if w in ("2025-10-08", "2025-10-06") else []),
        )
        for w, ans in waivers
    ]
    rows += [
        ("How many business days does expedited card delivery take? Number only.", ex("2"), "2"),
        (
            "How long does a standard replacement debit card take?",
            has("7 to 10 business days"),
            "7 to 10 business days",
        ),
        ("Can the member get an instant-issue card at the Plantation branch? Answer yes or no.", ex("no"), "no"),
        ("Can the member get an instant-issue card at the Davie branch? Answer yes or no.", ex("yes"), "yes"),
        (
            "The agent orders expedited delivery on Wednesday. On which weekday does the card arrive?",
            ex("Friday"),
            "Friday",
            HARD,
        ),
        (
            "The member asks what a travel notice is. What does the policy excerpt say?",
            has("member services"),
            unknown,
            HARD,
        ),
        (
            "The account ended the day $8.50 overdrawn after one item. What overdraft fee applies, in dollars?",
            num("$0"),
            "$0",
        ),
        (
            "The account ended the day $300 overdrawn after five items. What overdraft fees apply, in dollars?",
            num("$75.00"),
            "$75.00",
            HARD,
        ),
        (
            "The account ended the day $90 overdrawn after two items. What overdraft fees apply, in dollars?",
            num("$50.00"),
            "$50.00",
        ),
        (
            "The account ended the day exactly $10.00 overdrawn after one item. What overdraft fee applies, in "
            "dollars?",
            num("$0"),
            "$0",
            HARD,
        ),
        (
            "The account ended the day $10.01 overdrawn after one item. What overdraft fee applies, in dollars?",
            num("$25.00"),
            "$25.00",
            HARD,
        ),
        (
            "The member wants to wire money to a relative in Colombia. What is the fee, in dollars?",
            num("$45.00"),
            "$45.00",
        ),
        (
            "The member calls at 3:30 pm to send a domestic wire. Does it go out today? Answer yes or no.",
            ex("no"),
            "no",
            HARD,
        ),
        ("Can the member skip a mortgage payment with skip-a-pay? Answer yes or no.", ex("no"), "no"),
        (
            "The member has $1,200 in direct deposits a month. Do they pay the Premier Checking fee? Answer yes or no.",
            ex("no"),
            "no",
        ),
        (
            "What is the most Premier Checking refunds in other banks' ATM fees per month, in dollars?",
            num("$15"),
            "$15",
        ),
        ("Can a joint owner be added over the phone? Answer yes or no.", ex("no"), "no"),
    ]
    add(out, "agent", "agent-assist", agent, rows, 128, 3)

    disputes = [
        (
            "2026-09-02: Member's online banking password was changed from a new device on 2026-08-30, then a $640.00 "
            "online purchase she did not make posted. She still has her card.",
            "fraud",
        ),
        (
            "2026-09-02: Member agreed to pay $12.00 for a car wash at PLANTATION CAR WASH but was charged $120.00.",
            "merchant error",
        ),
        (
            "2026-09-02: Member stayed one night at a Key West hotel and the $219.00 charge posted twice on "
            "2026-08-28.",
            "duplicate",
        ),
        (
            "2026-09-02: Member's 16-year-old son used the card, with the member's permission, for $59.97 of in-game "
            "purchases; the member now disputes them.",
            "member error",
        ),
        (
            "2026-09-02: Member ordered a $310.00 patio set online on 2026-07-20; it never arrived and the seller "
            "stopped answering emails.",
            "merchant error",
        ),
        (
            "2026-09-02: Three ATM withdrawals of $400.00 were made in Orlando on 2026-08-29 while the member was at "
            "work in Fort Lauderdale with the card in her wallet. The card was used at a gas pump the day before.",
            "fraud",
        ),
        (
            "2026-09-02: Member disputes a $14.99 streaming charge, then remembers starting a free trial in July that "
            "turned into a paid plan.",
            "member error",
        ),
        ("2026-09-02: One $200.00 ATM withdrawal at the Davie branch ATM posted twice on 2026-08-31.", "duplicate"),
    ]
    hard_tags = {"member error"}  # authorized use by someone the member gave the card to, or a forgotten sign-up
    rows = [
        (dispute_file(rng, narr), "Tag this dispute: fraud, merchant error, duplicate or member error. Category only.",
         ex(tag), tag, *([HARD] if tag in hard_tags else []))
        for narr, tag in disputes
    ]  # fmt: skip
    for i, (ctx, q, ref, ans, *hard) in enumerate(rows, 5):
        c = case(f"dispute-{i:02d}", "dispute-triage", ctx, q, ref, ans, 64)
        c["tags"] += hard
        out.append(c)
    d1, d2 = date(2026, 9, 1), date(2026, 10, 5)
    deadlines = [
        (f"The member reported the dispute on Tuesday {d1}; the account is six years old. If the investigation is not "
         "finished, by what date must provisional credit be given? YYYY-MM-DD.",
         business_days_after(d1, 10).isoformat(), True),
        (f"A dispute on the member's new savings account (first deposit on 2026-09-20) was reported on Monday {d2}. By "
         "what date must provisional credit be given if the investigation is not finished? YYYY-MM-DD.",
         business_days_after(d2, 20).isoformat(), True),
        (f"An ACH debit dispute was reported on {d1} on a six-year-old account; provisional credit was given. By what "
         "date must the investigation be complete? YYYY-MM-DD.", (d1 + timedelta(days=45)).isoformat(), False),
        (f"A transfer initiated outside the United States was disputed on {d1}; provisional credit was given. By what "
         "date must the investigation be complete? YYYY-MM-DD.", (d1 + timedelta(days=90)).isoformat(), False),
        (f"The same new-account dispute (first deposit on 2026-09-20, reported on {d2}) got provisional credit. By "
         "what date must the investigation be complete? YYYY-MM-DD.",
         (d2 + timedelta(days=90)).isoformat(), True),
        ("The error first appeared on the statement sent on 2026-08-31. What is the last day the member can notify "
         "the credit union? YYYY-MM-DD.", (date(2026, 8, 31) + timedelta(days=60)).isoformat(), False),
        ("The investigation was completed on Tuesday 2026-11-24. By what date must the member be told the results? "
         "YYYY-MM-DD.", business_days_after(date(2026, 11, 24), 3).isoformat(), True),
        ("The member was notified of a reversal on Friday 2026-10-09. Through what date must checks and debits be "
         "honored? YYYY-MM-DD.", business_days_after(date(2026, 10, 9), 5).isoformat(), True),
    ]  # fmt: skip
    for i, (q, ans, hard) in enumerate(deadlines, 4):
        c = case(f"rege-{i:02d}", "dispute-triage", dispute_file(rng, disputes[0][0]), q, day(ans), ans, 64)
        c["tags"] += [HARD] if hard else []
        out.append(c)
    ctx = dispute_file(rng, disputes[1][0])
    add(out, "rege", "dispute-triage", ctx, [
        ("For how many years must the dispute records be kept? Number only.", ex("2"), "2"),
        ("The member disputes a charge on a credit card, not a debit card. Which procedure applies?",
         has("Regulation Z"), "the Regulation Z procedure", HARD),
    ], 64, 12)  # fmt: skip

    docs = [
        {"amount": 18900.00, "apr": 5.49, "months": 60, "first": date(2026, 11, 20), "contract": "AL-2026-120455",
         "branch": "Weston", "officer": 207, "borrower": "Andre Baptiste", "co": "Lucia Baptiste",
         "address": "2210 Bonaventure Blvd, Weston, FL 33326", "vehicle": "2023 Harbor Motors Breeze LX",
         "vin": "1HM0FICT2PA067891", "odometer": "24,950", "cash_price": "22,400.00", "down": "3,500.00",
         "doc_fee": "899.00", "gap": "GAP coverage declined.", "signed": "2026-10-05"},
        {"amount": 41275.00, "apr": 7.14, "months": 84, "first": date(2026, 12, 1), "contract": "AL-2026-121902",
         "branch": "Boca Raton", "officer": 318, "borrower": "Priya Raman", "co": "Kiran Raman",
         "address": "780 Glades Rd, Boca Raton, FL 33431", "vehicle": "2025 Harbor Motors Tidewater XLE",
         "vin": "1HM0FICT5SA004412", "odometer": "6,102", "cash_price": "45,775.00", "down": "5,000.00",
         "doc_fee": "999.00", "gap": "GAP coverage purchased for $595.00, paid in cash.", "signed": "2026-10-14"},
        {"amount": 9850.00, "apr": 8.99, "months": 48, "first": date(2026, 11, 10), "contract": "AL-2026-119377",
         "branch": "Hollywood", "officer": 133, "borrower": "Tomas Iglesias", "co": "Ana Iglesias",
         "address": "3301 Hollywood Blvd, Hollywood, FL 33021", "vehicle": "2019 Harbor Motors Pier S",
         "vin": "1HM0FICT9KA023318", "odometer": "61,744", "cash_price": "11,350.00", "down": "1,500.00",
         "doc_fee": "699.00", "gap": "GAP coverage declined.", "signed": "2026-10-08"},
        {"amount": 33600.00, "apr": 6.79, "months": 72, "first": date(2026, 12, 5), "contract": "AL-2026-122640",
         "branch": "Miramar", "officer": 256, "borrower": "Grace Okafor", "co": "Daniel Okafor",
         "address": "11400 Miramar Pkwy, Miramar, FL 33025", "vehicle": "2024 Harbor Motors Marina Sport",
         "vin": "1HM0FICT4RA058870", "odometer": "12,390", "cash_price": "38,100.00", "down": "4,500.00",
         "doc_fee": "999.00", "gap": "GAP coverage declined.", "signed": "2026-10-19"},
    ]  # fmt: skip
    n = 6
    for k, p in enumerate(docs):
        doc, f = loan_doc(rng, p)
        rows = [
            ("What is the amount financed?", has(f["amount"]), f["amount"]),
            ("What is the annual percentage rate?", has(f["apr"]), f["apr"]),
            ("What is the monthly payment?", has(f["payment"]), f["payment"]),
            ("Who is the co-borrower? Full name.", has(f["co"]), f["co"]),
            [
                ("What is the vehicle's VIN?", has(f["vin"]), f["vin"]),
                ("What is the finance charge?", has(f["finance"]), f["finance"]),
                ("When is the first payment due? YYYY-MM-DD.", day(f["first"]), f["first"]),
                (
                    "What is the balance after payment 12, from the amortization schedule? Amount only.",
                    has(f["balance_12"]),
                    f["balance_12"],
                    HARD,
                ),
            ][k],
        ]
        if k == 1:
            rows.append(("Is GAP coverage part of the amount financed? Answer yes or no.", ex("no"), "no", HARD))
        add(out, "loan", "loan-doc-extraction", doc, rows, 64, n)
        n += len(rows)

    cases_bsa = [
        (
            "26-0921-102", "retail employee, member since 2017, checking and savings.",
            "cash deposits near the CTR threshold.",
            [("2026-09-21", "Sunrise", 9900), ("2026-09-22", "Davie", 9850), ("2026-09-23", "Weston", 9950)],
            "Deposits were made at three different branches on consecutive days.", "2026-09-24",
            {"typology": "structuring", "total": "$29,700", "ctr": "no", "ctr_hard": False},
        ),
        (
            "26-0915-017", "restaurant owner, member since 2015, business-purpose personal checking.",
            "large cash deposit.", [("2026-09-15", "Plantation", 12500)],
            "Member said the cash is weekend receipts from the restaurant.", "2026-09-16",
            {"total": "$12,500", "ctr": "yes", "ctr_hard": False},
        ),
        (
            "26-0918-063", "contractor, member since 2020, checking.", "same-day cash at more than one branch.",
            [("2026-09-18", "Pembroke Pines", 6000), ("2026-09-18", "Sunrise", 5000)],
            "Both deposits were made by the member on the same business day.", "2026-09-21",
            {"total": "$11,000", "ctr": "yes", "ctr_hard": True},
        ),
        (
            "26-0925-088", "retiree, member since 2009, savings.", "cash deposit at the CTR threshold.",
            [("2026-09-25", "Weston", 10000)], "Member deposited proceeds from selling a boat to a neighbor.",
            "2026-09-28", {"total": "$10,000", "ctr": "no", "ctr_hard": True},
        ),
    ]  # fmt: skip
    n = 5
    for alert, profile, rule, entries, note, detected, f in cases_bsa:
        text = bsa_entries(rng, alert, profile, rule, entries, note)
        det = date.fromisoformat(detected)
        rows = [
            ("What is the total of the flagged cash deposits, in dollars?", num(f["total"]), f"{f['total']}.00"),
            ("Must a CTR be filed for this activity? Answer yes or no.", ex(f["ctr"]), f["ctr"],
             *([HARD] if f["ctr_hard"] else [])),
            (f"Activity was first detected on {detected} and a suspect is identified. By what date must a SAR be "
             "filed if the review supports suspicion? YYYY-MM-DD.", day((det + timedelta(days=30)).isoformat()),
             (det + timedelta(days=30)).isoformat()),
            (f"Activity was first detected on {detected} and no suspect is identified. What is the latest a SAR may "
             "be filed? YYYY-MM-DD.", day((det + timedelta(days=60)).isoformat()),
             (det + timedelta(days=60)).isoformat(), HARD),
        ]  # fmt: skip
        if "typology" in f:
            typology = ("Which typology fits the flagged cash activity? One word.", ex(f["typology"]), f["typology"])
            rows.insert(0, typology)
        else:
            rows.append(("May the analyst tell the member that a SAR is being considered? Answer yes or no.", ex("no"),
                         "no"))  # fmt: skip
        add(out, "bsa", "bsa-case-notes", text, rows, 128, n)
        n += len(rows)

    alerts = [
        (
            ["Alert FA-2026-10-0503, debit card ending 2209, score 874/999.",
             "2026-10-08 23:40  ONLINE BANKING  sign-in from a new device (Android, Jacksonville FL)",
             "2026-10-08 23:42  PROFILE  mobile phone number changed",
             "2026-10-08 23:47  TRANSFER  $1,950.00 to external account ending 7731",
             "2026-10-08 23:49  TRANSFER  $1,900.00 to external account ending 7731  held by rule ATO-2"],
            [("Which pattern does the alert show? Answer with the pattern name.", has("account takeover"),
              "account takeover"),
             ("What changed on the profile before the transfers?", has("phone number"), "the mobile phone number"),
             ("What was the first transfer amount, in dollars?", num("$1,950.00"), "$1,950.00"),
             ("How much was transferred or attempted in total, in dollars?", num("$3,850.00"), "$3,850.00", HARD),
             ("Which external account received the transfers? Last four digits.", ex("7731"), "7731")],
        ),
        (
            ["Alert FA-2026-10-0511, debit card ending 2209, score 801/999.",
             "2026-10-09 13:05  CARD PRESENT  BAYSIDE ELECTRONICS   MIAMI FL     612.40  approved",
             "2026-10-09 13:31  CARD PRESENT  SOUTH BEACH JEWELERS  MIAMI FL   1,240.00  declined by rule CP-GEO",
             "Member's card history is in Broward and Palm Beach counties; the member reported the card missing at "
             "14:10."],
            [("Which pattern does the alert show? Answer with the pattern name.", has("lost or stolen"),
              "lost or stolen card"),
             ("In which city were the alert transactions? City only.", ex("Miami"), "Miami"),
             ("What was the approved amount, in dollars?", num("$612.40"), "$612.40"),
             ("Were the alert transactions card present or online? Answer card present or online.",
              ex("card present"), "card present"),
             ("At what time did the member report the card missing? HH:MM.", ex("14:10"), "14:10")],
        ),
        (
            ["Alert FA-2026-10-0520, debit card ending 2209, score 655/999.",
             "2026-10-10 09:12  DISPUTE  member disputes 6 card-present purchases at PUBLIX #1132 totalling $412.66",
             "Each disputed purchase was made with the chip and PIN, at the member's usual store, during the same "
             "hours as the member's undisputed purchases; the card was never reported lost."],
            [("Which pattern does the alert show? Answer with the pattern name.", has("first-party misuse"),
              "first-party misuse", HARD),
             ("How many purchases are disputed? Number only.", ex("6"), "6"),
             ("What is the total disputed, in dollars?", num("$412.66"), "$412.66"),
             ("At which merchant were the disputed purchases made?", has("PUBLIX"), "PUBLIX #1132"),
             ("Was the card reported lost? Answer yes or no.", ex("no"), "no")],
        ),
        (
            ["Alert FA-2026-10-0537, debit card ending 2209, score 905/999.",
             "2026-10-11 03:02  ONLINE  GIFTLY*CARDS    US  0.50  approved",
             "2026-10-11 03:03  ONLINE  GIFTLY*CARDS    US  0.50  approved",
             "2026-10-11 03:03  ONLINE  GIFTLY*CARDS    US  0.50  approved",
             "2026-10-11 03:04  ONLINE  GIFTLY*CARDS    US  0.50  approved",
             "2026-10-11 03:04  ONLINE  GIFTLY*CARDS    US  0.50  approved",
             "2026-10-11 03:11  ONLINE  LUXE SNEAKER LAB US  1,129.00  declined by rule CNP-HIGH"],
            [("Which pattern does the alert show? Answer with the pattern name.", has("card testing"), "card testing"),
             ("How many test authorizations preceded the large purchase? Number only.", ex("5"), "5"),
             ("What was each test authorization for, in dollars?", num("$0.50"), "$0.50"),
             ("How much did the test authorizations total, in dollars?", num("$2.50"), "$2.50", HARD),
             ("At which merchant was the large purchase attempted?", has("LUXE SNEAKER LAB"), "LUXE SNEAKER LAB")],
        ),
    ]  # fmt: skip
    n = 6
    for alert, rows in alerts:
        add(out, "fraud", "fraud-alert-narratives", fraud_alert(rng, alert), rows, 256, n)
        n += len(rows)

    n = 4
    for header, section, f in NOTICES:
        notice = reg_notice(rng, header, section)
        if section == "748":
            rows = [
                ("Which agency issued the notice?", has(f["agency"]), f["agency"]),
                ("Which part of 12 CFR would be amended? Number only.", ex(f["part"]), f["part"]),
                ("When are comments due? YYYY-MM-DD.", day(f["comments"]), f["comments"]),
                ("What is the proposed effective date? YYYY-MM-DD.", day(f["effective"]), f["effective"]),
                (
                    "Within how many hours of discovery must a qualifying cyber incident be reported? Number only.",
                    ex(f["report_hours"]),
                    f["report_hours"],
                ),
                (
                    "Cypress Harbor has $1.4 billion in assets. How often must it test its incident response plan, in "
                    "months? Number only.",
                    ex(f["test_months_large"]),
                    f["test_months_large"],
                    HARD,
                ),
                (
                    "A credit union with $300 million in assets: how often must it test, in months? Number only.",
                    ex(f["test_months_small"]),
                    f["test_months_small"],
                    HARD,
                ),
            ]
        elif section == "1026":
            rows = [
                ("Which regulation does the rule amend?", has(f["regulation"]), f["regulation"]),
                ("Is this a proposed rule or a final rule?", has(f["kind"]), f["kind"]),
                ("When is the rule effective? YYYY-MM-DD.", day(f["effective"]), f["effective"]),
                ("When does compliance become mandatory? YYYY-MM-DD.", day(f["mandatory"]), f["mandatory"], HARD),
                (
                    "How many days before a promotional rate expires must the alert be sent? Number only.",
                    ex(f["days"]),
                    f["days"],
                ),
                (
                    "Above how many open credit card accounts does the rule apply? Number only.",
                    ("regex", r"\b10,?000\b"),
                    f["accounts"],
                ),
                ("Are comments requested? Answer yes or no.", ex("no"), "no", HARD),
            ]
        else:
            rows = [
                ("Which agency issued the notice?", has(f["agency"]), f["agency"]),
                ("Which part of 31 CFR would be amended? Number only.", ex(f["part"]), f["part"]),
                ("When are comments due? YYYY-MM-DD.", day(f["comments"]), f["comments"]),
                ("What is the current recordkeeping threshold, in dollars?", num(f["old"]), f["old"]),
                ("What would the new threshold be, in dollars?", num(f["new"]), f["new"]),
                ("For how many years would the records be kept? Number only.", ex(f["years"]), f["years"]),
                (
                    "Would a $1,000 transfer between two Florida accounts be covered by the new threshold? Answer "
                    "yes or no.",
                    ex("no"),
                    "no",
                    HARD,
                ),
            ]
        add(out, "reg", "reg-change-digest", notice, rows, 128, n)
        n += len(rows)

    code = code_context(rng)
    add(out, "code", "code-assistant", code, [
        ('What does overdraft_fees(Decimal("-50"), 5) return? The value as str() prints it.', ex("75.000"), "75.000",
         HARD),
        ('What does overdraft_fees(Decimal("-10.00"), 2) return? The value as str() prints it.', ex("0.00"), "0.00",
         HARD),
        ('What does overdraft_fees(Decimal("-10.01"), 1) return? The value as str() prints it.', ex("25.000"),
         "25.000", HARD),
        ('What does overdraft_fees(Decimal("-9.99"), 4) return? The value as str() prints it.', ex("0.00"), "0.00"),
        ("What does paper_statement_fee(65, False) return? The value as str() prints it.", ex("0.00"), "0.00"),
        ("What does paper_statement_fee(23, True) return? The value as str() prints it.", ex("0.00"), "0.00"),
        ("What does paper_statement_fee(24, True) return? The value as str() prints it.", ex("2.00"), "2.00", HARD),
        ("What does paper_statement_fee(64, False) return? The value as str() prints it.", ex("2.00"), "2.00"),
        ('What HTTP status does gateway_status("pii_blocked") return? Number only.', ex("422"), "422"),
        ('What HTTP status does gateway_status("rate_limited") return? Number only.', ex("429"), "429"),
        ('What HTTP status does gateway_status("all_circuits_open") return? Number only.', ex("503"), "503"),
        ('What HTTP status does gateway_status("policy_denied") return? Number only.', ex("403"), "403"),
        ('Which exception does gateway_status("timeout") raise? Exception name only.', ex("KeyError"), "KeyError",
         HARD),
        ("What is the value of MAX_OVERDRAFT_FEES_PER_DAY? Number only.", ex("3"), "3"),
        ("Which rounding mode does round_cents use? Constant name only.", ex("ROUND_HALF_UP"), "ROUND_HALF_UP"),
        ("How many places in the module raise FeeError? Number only.", ex("0"), "0", HARD),
        ("How many test functions are in the tests excerpt? Number only.", ex("80"), "80"),
        ("Would any excerpted test fail if overdraft_fees returned the wrong amount? Answer yes or no.", ex("no"),
         "no", HARD),
        ('What does round_cents(Decimal("1.005")) return? The value as str() prints it.', ex("1.005"), "1.005", HARD),
        ("What should round_cents pass to quantize to round to cents? The Decimal literal.", has('Decimal("0.01")'),
         'Decimal("0.01")'),
        ("What is the value of DE_MINIMIS? The Decimal string only.", ex("10.00"), "10.00"),
        ("What does overdraft_fees(Decimal(\"0\"), 3) return? The value as str() prints it.", ex("0.00"), "0.00"),
    ], 64, 3)  # fmt: skip

    add(out, "marketing", "content-drafts", RATES, [
        ("What is the Money Market APY?", has("3.10%"), "3.10%"),
        ("What is the dividend rate, not the APY, on the 12-month certificate?", has("4.07%"), "4.07%", HARD),
        ("What is the 18-month certificate APY?", has("4.05%"), "4.05%"),
        ("What is the 6-month certificate APY?", has("3.90%"), "3.90%"),
        ("What is the 36-month certificate APY?", has("3.75%"), "3.75%"),
        ("What is the Holiday Club APY?", has("0.50%"), "0.50%"),
        ("What is the Money Market minimum, in dollars?", num("$2,500"), "$2,500"),
        ("What is the certificate minimum, in dollars?", num("$500"), "$500"),
        ("May a rate ad say \"guaranteed\"? Answer yes or no.", ex("no"), "no"),
        ("What must appear in the footer of every ad that mentions a deposit product?",
         has("Federally insured by NCUA"), "Federally insured by NCUA"),
        ("Which word do ads use for what savings earn?", ex("dividends"), "dividends"),
        ("What short URL do print ads include?", has("cypressharbor.example/rates"), "cypressharbor.example/rates"),
        ("What case are headlines written in?", has("sentence case"), "sentence case"),
        ("Who reviews the Spanish version before release?", has("native speaker"), "a native speaker"),
        ("Does the draft state the certificate's minimum deposit? Answer yes or no.", ex("yes"), "yes"),
        ("Does the Money Market APY in the email match the rate sheet? Answer matches or differs.", ex("matches"),
         "matches"),
        ('A new draft says "12-month certificate, 4.07% APY". Does it match the rate sheet? Answer matches or '
         "differs.", ex("differs"), "differs", HARD),
        ('A new draft says "18-month certificate, 4.15% APY". Does it match the rate sheet? Answer matches or '
         "differs.", ex("differs"), "differs", HARD),
        ('May an ad say "best rate in Florida"? Answer yes or no.', ex("no"), "no"),
        ("How many branches does the draft mention? Number only.", ex("11"), "11"),
        ("On what date is the rate sheet effective? YYYY-MM-DD.", day("2026-10-01"), "2026-10-01"),
        ("Is the certificate rate fixed for the term? Answer yes or no.", ex("yes"), "yes"),
    ], 64, 3)  # fmt: skip
    return out


def render(cases: list[dict]) -> str:
    head = (
        "# Route-eval cases for scripts/eval_routes.py, written by scripts/build_eval_cases.py: tasks of the fictional "
        "Cypress Harbor Credit Union, tagged with the app that does them. Every name, number and document is invented."
    )
    return head + "\n" + "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed file is stale")
    args = ap.parse_args(argv)
    text = render(build())
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is stale: run python scripts/build_eval_cases.py")
            return 1
        print(f"{OUT.relative_to(ROOT)} is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}: {len(build())} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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


def loan_doc(rng: random.Random) -> tuple[str, dict]:
    amount, apr, months = 28450.00, 6.24, 72
    r = apr / 100 / 12
    payment = round(amount * r / (1 - (1 + r) ** -months), 2)
    first = date(2026, 11, 15)
    schedule = []
    bal = amount
    for i in range(months):
        interest = round(bal * r, 2)
        principal = round(payment - interest, 2)
        bal = round(bal - principal, 2)
        due = date(first.year + (first.month - 1 + i) // 12, (first.month - 1 + i) % 12 + 1, 15)
        schedule.append(
            f"{i + 1:>3}  {due.isoformat()}  {payment:>8.2f}  {interest:>7.2f}  {principal:>8.2f}  {max(bal, 0):>10.2f}"
        )
    text = (
        f"""You extract fields from loan documents for Cypress Harbor Credit Union's lending team (fictional). Answer
with the value only, as it appears in the document unless the question asks for a format.

# Document: Motor Vehicle Retail Installment Contract and Security Agreement (scanned, OCR text)

Contract number: AL-2026-118734        Branch: Coral Springs        Loan officer: staff 412
Borrower: Marisol Vega                  Co-borrower: Dana Ortiz
Address: 1450 NW 99th Ave, Coral Springs, FL 33071 (fictional)

FEDERAL TRUTH-IN-LENDING DISCLOSURES
ANNUAL PERCENTAGE RATE: {apr:.2f}%      FINANCE CHARGE: ${payment * months - amount:,.2f}
Amount Financed: ${amount:,.2f}         Total of Payments: ${payment * months:,.2f}
Payment schedule: {months} monthly payments of ${payment:,.2f} beginning November 15, 2026, and on the 15th day of
each month after that.

Collateral: 2024 Harbor Motors Coastline SE, VIN 1HM0FICT1RA012345, odometer 18,214 miles.
Itemization of amount financed: cash price $31,950.00; down payment $4,500.00; documentary fee $999.00 (paid in
cash); title and registration $0.00 financed; GAP coverage declined.

Late charge: if a payment is more than 10 days late, a late charge of $20.00 may be assessed.
Prepayment: if you pay off early, you will not have to pay a penalty.
Security interest: you are giving a security interest in the vehicle being purchased.
Insurance: you must keep the vehicle insured against physical damage, naming Cypress Harbor Credit Union as loss
payee. If you do not, we may buy insurance and add its cost to the loan.

Governing law: Florida. Arbitration: either party may elect arbitration of any claim, except small-claims matters.
Signatures: Marisol Vega 2026-10-02; Dana Ortiz 2026-10-02.

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
        "co": "Dana Ortiz",
        "vin": "1HM0FICT1RA012345",
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


def reg_notice(rng: random.Random) -> str:
    topics = [
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
            f"preliminarily concludes that the notice requirement in proposed section 1005.{rng.randrange(11, 35)} "
            "should apply uniformly, while seeking comment on alternatives for institutions with fewer than "
            f"{rng.choice([50, 100, 250])} employees and on the cost of compliance for each alternative."
        )
    return REG_NOTICE + "\nSUPPLEMENTARY INFORMATION:\n\n" + "\n\n".join(paras)


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

# Payroll Settlement Bot

A Telegram bot that settles payroll using **balances**, not person-to-person debts.

The system does not track "John owes Mike $500." It tracks three independent facts:

| | |
|---|---|
| **Payable** | John owes $500 *into* the payroll system |
| **Receivable** | Mike is owed $300 *by* the payroll system |
| **Settlement** | John should send $300 directly to Mike |

A settlement is only a **routing instruction** used to discharge a payable and a
receivable at the same time. John never owed Mike anything; the bot simply worked
out that one bank transfer satisfies two separate obligations.

```
John owes:      $500          John → Mike:  $300
Mike is owed:   $300    ⟶     John → Sarah: $200
Sarah is owed:  $200
                              After verification, all three balances are zero.
```

> The bot **never holds or transfers money.** It records balances, generates
> payment routes, tracks claimed payments, gathers confirmations, lets an
> administrator verify them, and maintains the ledger. People pay each other
> directly.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add your bot token and admin Telegram IDs
python demo.py                # see the whole lifecycle, no token needed
python main.py                # run the bot
```

`demo.py` runs the full payroll cycle against an in-memory database and prints
the ledger at every step. It is the fastest way to see the accounting rules in
action.

---

## The accounting rule that matters most

**Generating a settlement never moves a finalized balance. Only administrator
verification does.**

Every payable and receivable carries four distinct quantities. Conflating any
two of them corrupts the ledger:

| Quantity | Meaning |
|---|---|
| `original` | What was entered for this payroll period |
| `reserved` | Routed to a settlement, but not yet verified — work in progress |
| `verified` | Admin-verified. The only figure that discharges the obligation |
| `remaining` | `original − verified`. What is still genuinely owed |
| `available` | `original − verified − reserved`. What may still be routed |

Worked through:

```
John owes $1,000, routed as a $600 and a $400 settlement.

Before any payment:      original 1000 | reserved 1000 | verified   0 | remaining 1000
Payer marks both paid:   original 1000 | reserved 1000 | verified   0 | remaining 1000
Recipient confirms:      original 1000 | reserved 1000 | verified   0 | remaining 1000
Admin verifies the 600:  original 1000 | reserved  400 | verified 600 | remaining  400
The 400 is cancelled:    original 1000 | reserved    0 | verified 600 | remaining  400
                                                          ↳ and 400 is available again
```

`reserved` and `verified` are **derived from settlement status**, never stored as
mutable counters. Cancelling a settlement frees its capacity automatically —
there is no balance arithmetic that can drift out of step.

### Money

All currency values are `Decimal`, quantized to two places, stored in SQLite as
exact integer minor units. `payroll_bot.money.money()` **rejects `float` outright**
rather than silently importing binary rounding error into a financial ledger.

---

## Commands

### Everyone

| Command | Purpose |
|---|---|
| `/start`, `/help` | Introduction and your command list |
| `/balance` | What you owe and what you're owed, with live progress |
| `/methods` | Manage your Venmo / Cash App / Zelle / PayPal handles |

### Administrators

| Command | Purpose |
|---|---|
| `/payroll` | Enter `OWES` / `OWED` balances |
| `/dashboard` | Current payroll overview, with drill-down buttons |
| `/generate` | Build a settlement plan (preview only) |
| `/verify` | Review payments awaiting verification |
| `/user @handle` | One person's full position |
| `/reassign <id> to @user [amount]` | Manually re-route a settlement |
| `/cancelsettlement <id> [reason]` | Free an amount for reassignment |
| `/priority @handle <level>` | Recipient priority for the matcher |
| `/promote @handle` | Grant admin rights |
| `/newpayroll [label]` | Start a new batch |
| `/audit [settlement_id]` | Immutable history |

---

## Workflows

### Entering payroll

Admins enter *balances*, never relationships:

```
OWES
@john 500
@chris 400
@david 250

OWED
@mike 300
@sarah 600
@alex 250
```

The bot totals both sides and reports the difference. If they don't match it
refuses to proceed silently:

```
⚠️ PAYROLL DOES NOT BALANCE
Total Owed: $5,420
Total Receivable: $5,370
Difference: $50
```

Settlements are **not** generated until the admin fixes the discrepancy or
explicitly confirms it. Unreadable lines are reported by line number rather than
skipped — a silently dropped row is a payroll that balances on screen and not in
reality.

### Generating and approving a plan

`Generate Settlements` computes a plan and shows it as a **preview**. Nothing is
persisted and nothing reaches a user until the admin approves:

```
PAYROLL SETTLEMENT PLAN
2 people owe money
3 people are owed money
Total: $1,100.00
Generated Transfers: 4

@john  → @mike   $500.00  Venmo
@chris → @sarah  $350.00  Zelle
@john  → @alex   $200.00  Venmo
@chris → @alex    $50.00  ⚠️

[✅ Approve Settlement Plan]  [🔄 Recalculate]  [✏️ Edit]  [❌ Cancel]
```

On approval every proposal is re-validated against live capacity — a preview
generated minutes ago cannot be forced through if a balance moved in between.

### Verification

```
1. Payer sends money directly to the recipient
2. Payer presses "Mark Paid" (may add a reference ID and a screenshot)
3. Recipient is asked to confirm  →  [✅ Received]  [❌ Not Received]
4. Administrator reviews          →  [✅ VERIFY]  [❌ REJECT]  [⚠️ DISPUTE]
```

Only step 4 moves finalized balances.

```
     PENDING ──payer marks paid──> PAYER_MARKED_PAID
                                          │
                          recipient ──────┼────── recipient
                          confirms        │        denies
                                          ▼            ▼
                              RECIPIENT_CONFIRMED   RECIPIENT_DENIED
                                          │            │
                                          └──── admin ─┘
                                                │
                            ┌───────────────────┼───────────────────┐
                            ▼                   ▼                   ▼
                        VERIFIED            REJECTED            DISPUTED
```

`REJECTED` and `CANCELLED` release their reservation, so the amount becomes
available for reassignment. `DISPUTED` keeps reserving — the question is still
open, so the money must not be double-routed.

---

## Settlement strategies

Strategies do not each reimplement a matching loop. The engine owns one greedy
loop and asks strategies to **score** candidate pairings, so any set of them can
be combined by weighted sum.

```python
class SettlementStrategy(ABC):
    @abstractmethod
    def score_pair(self, payer, recipient, context) -> float: ...
    def propose_amount(self, payer, recipient, context) -> Decimal | None: ...
```

| Strategy | Behaviour |
|---|---|
| `MinimumTransfersStrategy` | Prefers exact pairings (clears two parties in one transfer), then the largest chunk |
| `PaymentCompatibilityStrategy` | Prefers payer/recipient pairs sharing a payment method |
| `AdminPriorityStrategy` | Pays higher-priority recipients first |
| `MaxTransferSizeStrategy` | Caps individual transfers, e.g. under an app's limit |

Combine them with `CompositeStrategy`:

```python
strategy = CompositeStrategy(
    WeightedStrategy(PaymentCompatibilityStrategy(), 3.0),
    WeightedStrategy(AdminPriorityStrategy(), 2.0),
    WeightedStrategy(MinimumTransfersStrategy(), 1.0),
)
```

A `FORBIDDEN` score from any component vetoes the pair outright — a hard
constraint, not an opinion to be outvoted.

**Payment compatibility.** If John has Venmo and Cash App, Mike accepts Venmo and
Sarah accepts only Zelle, the matcher routes John → Mike. Where no compatible
counterpart exists the settlement is still created but flagged for administrator
review, rather than being silently dropped. Set `STRICT_PAYMENT_METHODS=true` to
refuse such pairings instead.

**Minimum transfers** is the standard greedy heuristic. A provably minimal set is
NP-hard (it reduces to subset-sum); greedy guarantees at most
`payers + recipients − 1` transfers, which is what a payroll admin actually cares
about.

---

## Architecture

```
payroll_bot/
├── money.py            Decimal handling; rejects float
├── models.py           SQLAlchemy schema; money stored as exact integer cents
├── ledger.py           Balance arithmetic and capacity validation
├── matching.py         Greedy bipartite matching engine
├── parsing.py          OWES/OWED block parser
├── audit.py            Append-only history
├── config.py           Environment configuration
├── db.py               Engine and transactional session scope
├── strategies/
│   ├── base.py         SettlementStrategy interface + CompositeStrategy
│   └── builtin.py      The four built-in strategies
├── services/           All business logic
│   ├── payroll.py      Batches, ledger entry, plan generation and approval
│   ├── settlement.py   Lifecycle state machine, reassignment, disputes
│   └── accounts.py     Users, admin rights, payment methods
└── bot/                Telegram layer only
    ├── app.py          Application wiring
    ├── views.py        Pure rendering functions
    ├── keyboards.py    Inline keyboards and callback encoding
    ├── notifications.py Outbound messages
    └── handlers/       Thin: authenticate, parse, call a service, render
```

Handlers contain no financial logic. Everything that touches money lives in
`services/`, which makes the rules testable without a Telegram connection — the
67-test suite never starts a bot.

Each mutation runs inside `session_scope()`, which commits on success and rolls
back on any exception, so a settlement can never be created without its audit
row.

### Audit log

Every financial event is appended: payroll created, payable added, receivable
added, balance modified, settlement generated, reassigned, marked paid,
confirmed, verified, rejected, cancelled, disputed, resolved. Rows are never
updated or deleted — a correction is recorded as a new entry describing the
correction, so any payroll period can be replayed.

---

## Tests

```bash
python -m pytest
```

67 tests covering the spec's worked examples, the accounting invariants
(generation doesn't move balances; verification does; cancellation frees
capacity), the state machine and its authorization rules, exact-decimal
arithmetic through a database round trip, and the parser's error reporting.

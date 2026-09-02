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
| `/whoami` | Your Telegram ID, username and admin status |

### Administrators

| Command | Purpose |
|---|---|
| `/payroll` | Enter `OWES` / `OWED` balances |
| `/dashboard` | Current payroll overview, with drill-down buttons |
| `/generate` | Build a settlement plan (preview only) |
| `/queue` | Everyone waiting to be paid, longest wait first |
| `/next [@payer]` | Step through the queue, with skip and assign |
| `/setmethod @handle venmo @Theirs` | Record a payment method for someone else |
| `/delmethod @handle <id>` | Remove one |
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

**The two sides do not have to match.** People are routinely owed money before
the payers covering them have settled up, so a shortfall is a schedule rather
than an error. The bot totals both sides and describes the gap:

```
ℹ️ $500.00 more is owed out than has come in. The queue pays people in the
order they were added, so this covers the front of the line first and the
rest waits for later payers.
```

Unreadable lines *are* still reported, by line number, and no payroll
containing an error is applied even partially — a silently dropped row is a
payroll that reconciles on screen and not in reality.

### The payment queue

Because a payroll need not balance, something has to decide who gets paid
first. People are paid in the order they were added:

```
PAYMENT QUEUE
3 waiting · $900.00 still owed
Longest wait first.

 1 ▸ @mike — $300.00  today
 2 ▸ @sarah — $400.00  today
 3 ▸ @alex — $200.00  today

▸ = still needs someone assigned to pay them
```

Position is **derived from creation order, never stored**, so paying someone
off moves everyone behind them up automatically and no field can drift out of
step with the ledger. Partially paid people keep their place, and someone whose
amount is routed but not yet verified stays queued — routed money is not
received money.

`/next` steps through the queue one person at a time, showing every payment
method they accept. Skipping wraps around, so an admin cycling to find someone
a given payer can actually pay never reaches a dead end:

```
NEXT IN LINE · 1 of 3

@mike
Still owed: $300.00
added today

Pay them with:
  Venmo: @MikeExample  ✅

@john can pay them by Venmo.

[✅ Assign this payment]  [⬅️ Previous]  [Skip ➡️]
```

`/next @john` marks which methods the two share and offers to assign the
payment. Manual assignment goes through the same capacity checks as a generated
plan — a hand-picked pairing gets no licence to overdraw a balance.

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
├── queue.py            Who gets paid next, derived from creation order
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
107-test suite never starts a bot.

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

107 tests covering the spec's worked examples, the accounting invariants
(generation doesn't move balances; verification does; cancellation frees
capacity), the state machine and its authorization rules, exact-decimal
arithmetic through a database round trip, and the parser's error reporting.

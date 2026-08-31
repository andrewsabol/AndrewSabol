"""A Telegram bot that settles payroll using balances, not person-to-person debts.

Users owe money *into* the system or are owed money *by* the system. Settlements
are generated routing instructions that discharge both sides at once; they are
never treated as the underlying debt.
"""

__version__ = "1.0.0"

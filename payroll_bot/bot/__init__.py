"""Telegram interface layer.

Handlers are deliberately thin: they authenticate, parse arguments, call the
service layer, and render. All financial logic lives in ``payroll_bot.services``.
"""

"""Invoice generation."""
from . import billing


def make_invoice(total):
    return {"total": total, "currency": billing.PRICE and "EUR"}


def refund(invoice):
    from app.db import connect
    return connect(invoice["url"])

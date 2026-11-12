"""Charging customers."""
from app.services import invoices
from app.utils import slugify

PRICE = 3


def charge(amount):
    return invoices.make_invoice(amount * PRICE)

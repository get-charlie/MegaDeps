"""Price calculations."""
from shop.helpers import *


class Discount:
    factor = 0.9


class Rule:
    def check(self, product):
        raise NotImplementedError


def total(products):
    return round_price(sum(p.price for p in products))

"""Product model."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shop.pricing import Discount


class Product:
    def __init__(self, name, price):
        self.name = name
        self.price = price

    def apply(self, discount: "Discount"):
        return self.price * discount.factor

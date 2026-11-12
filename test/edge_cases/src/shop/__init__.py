"""Shop package: re-exports the public API."""
from .models import Product
from .pricing import total

__all__ = ["Product", "total"]

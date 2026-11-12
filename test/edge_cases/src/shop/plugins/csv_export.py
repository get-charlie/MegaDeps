"""Plugin in a namespace package (no __init__.py)."""
import shop


def export(products):
    return "\n".join(f"{p.name},{shop.total([p])}" for p in products)

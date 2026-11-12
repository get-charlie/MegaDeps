"""Command-line entry point next to src/."""
from shop import Product
from shop.plugins.csv_export import export

print(export([Product("pen", 1.5)]))

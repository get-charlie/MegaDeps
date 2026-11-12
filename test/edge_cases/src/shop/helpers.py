"""Formatting helpers used through a star import."""


def round_price(value):
    return round(value, 2)


def unused_formatter(value):
    return f"{value:.2f}"

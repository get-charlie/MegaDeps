"""Report formatting helpers."""


def format_report(data):
    return ", ".join(f"{key}={value}" for key, value in sorted(data.items()))

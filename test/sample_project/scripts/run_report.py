"""Command-line report script."""
import helpers

from app.main import run


if __name__ == "__main__":
    print(helpers.format_report({"result": run()}))

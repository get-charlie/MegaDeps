"""Application entry point."""
import sys

import app.utils
from app import config
from app.db import connect, Session
from .services import billing

HANDLERS = {}


def register(func):
    HANDLERS[func.__name__] = func
    return func


@register
def on_start():
    print("starting")


def run():
    settings = config.load()
    conn = connect(settings.url)
    worker = Worker(conn)
    worker.start()
    return billing.charge(10)


class Worker:
    def __init__(self, conn):
        self.session = Session(conn)

    def start(self):
        self.session.begin()

    def stop(self):
        self.session.close()


def main():
    print(app.utils.describe())
    sys.exit(run())


if __name__ == "__main__":
    main()

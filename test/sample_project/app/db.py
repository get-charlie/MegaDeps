"""Database access."""
import requests

from .utils import slugify, retry


def connect(url):
    return retry(lambda: requests.get(slugify(url)))


class Session:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return self.conn

    def close(self):
        self.conn = None

    def rollback_all(self):
        raise NotImplementedError

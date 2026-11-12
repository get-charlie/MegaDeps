"""Configuration loading."""
from app import utils


class Config:
    def __init__(self, url):
        self.url = url


def load():
    return Config(utils.slugify("sqlite://local"))


def legacy_loader(path):
    with open(path) as fh:
        return Config(fh.read())

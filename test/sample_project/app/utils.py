"""Small helpers."""


def slugify(text):
    return text.lower().replace(" ", "-")


def retry(action, attempts=3):
    for _ in range(attempts):
        try:
            return action()
        except OSError:
            continue
            print("retrying")
    return None


def describe():
    from app import config
    return config.load().url


def old_helper():
    return 42

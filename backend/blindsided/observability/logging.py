import logging
import os


DEFAULT_LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def parse_log_level(value: str | None) -> int:
    if not value:
        return DEFAULT_LOG_LEVEL
    parsed = getattr(logging, value.strip().upper(), None)
    return parsed if isinstance(parsed, int) else DEFAULT_LOG_LEVEL


def configure_logging() -> None:
    """Configure one consistent process-wide logging format."""
    level = parse_log_level(os.getenv("LOG_LEVEL"))
    root = logging.getLogger()
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    root.setLevel(level)

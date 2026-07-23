import logging
from unittest import mock

from blindsided.observability.logging import (
    DEFAULT_LOG_LEVEL,
    configure_logging,
    parse_log_level,
)
from backend.tests.helpers import BackendTestCase


class RuntimeLoggingTests(BackendTestCase):
    def test_log_level_defaults_to_info_and_accepts_override(self):
        self.assertEqual(parse_log_level(None), DEFAULT_LOG_LEVEL)
        self.assertEqual(parse_log_level("debug"), logging.DEBUG)
        self.assertEqual(parse_log_level("not-a-level"), DEFAULT_LOG_LEVEL)

    def test_configure_logging_does_not_add_duplicate_handlers(self):
        root = logging.getLogger()
        previous_handlers = list(root.handlers)
        previous_level = root.level
        root.handlers = []
        try:
            with mock.patch.dict("os.environ", {"LOG_LEVEL": "DEBUG"}):
                configure_logging()
                first_handlers = list(root.handlers)
                configure_logging()
            self.assertEqual(root.level, logging.DEBUG)
            self.assertEqual(root.handlers, first_handlers)
            self.assertEqual(len(root.handlers), 1)
        finally:
            root.handlers = previous_handlers
            root.setLevel(previous_level)

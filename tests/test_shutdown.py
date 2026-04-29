from __future__ import annotations

from unittest.mock import patch

from skr_crypto.server.shutdown import mark_started, seconds_remaining, uptime


class TestShutdownTimers:
    def test_uptime_increases(self):
        mark_started()
        u1 = uptime()
        assert u1 >= 0

    def test_seconds_remaining(self):
        mark_started()
        remaining = seconds_remaining()
        assert 0 < remaining <= 600

    @patch("skr_crypto.server.shutdown.time.time")
    def test_uptime_calculation(self, mock_time):
        mock_time.return_value = 1000.0
        mark_started()
        mock_time.return_value = 1030.0
        assert uptime() == 30

    @patch("skr_crypto.server.shutdown.time.time")
    def test_seconds_remaining_calculation(self, mock_time):
        mock_time.return_value = 1000.0
        mark_started()
        mock_time.return_value = 1100.0
        assert seconds_remaining() == 500  # 600 - 100

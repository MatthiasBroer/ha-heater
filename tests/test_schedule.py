"""Tests for the standalone weekly schedule engine."""
import unittest
from datetime import datetime, timezone

from custom_components.heater.schedule import active_period, next_period


class TestWeeklySchedule(unittest.TestCase):
    def setUp(self):
        self.entries = [{
            "days": [6],  # Sunday
            "start": "11:00:00",
            "end": "15:00:00",
            "temperature": 20.0,
        }]

    def test_active_at_start(self):
        now = datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)
        self.assertEqual(active_period(now, self.entries), self.entries[0])

    def test_active_before_end(self):
        now = datetime(2026, 11, 8, 14, 59, tzinfo=timezone.utc)
        self.assertEqual(active_period(now, self.entries), self.entries[0])

    def test_inactive_at_end(self):
        now = datetime(2026, 11, 8, 15, 0, tzinfo=timezone.utc)
        self.assertIsNone(active_period(now, self.entries))

    def test_inactive_on_wrong_day(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(active_period(now, self.entries))

    def test_finds_next_sunday(self):
        now = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)
        result = next_period(now, self.entries)

        self.assertIsNotNone(result)
        start, entry = result
        self.assertEqual(start, datetime(
            2026, 11, 15, 11, 0, tzinfo=timezone.utc
        ))
        self.assertEqual(entry["temperature"], 20.0)

    def test_multiple_periods(self):
        entries = self.entries + [{
            "days": [6],
            "start": "18:00:00",
            "end": "22:00:00",
            "temperature": 21.0,
        }]
        now = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)

        result = next_period(now, entries)

        self.assertIsNotNone(result)
        start, entry = result
        self.assertEqual(start.hour, 18)
        self.assertEqual(entry["temperature"], 21.0)


if __name__ == "__main__":
    unittest.main()

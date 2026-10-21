import unittest
from datetime import date, datetime, timedelta, timezone

from app import active_flight_occurrence, flight_occurrence, flight_valid_on, next_flight_occurrence


def flight(**changes):
    return {
        'frequency': '1234567', 'departure_time': '10:00', 'arrival_time': '12:00',
        'origin_timezone': 'UTC', 'destination_timezone': 'UTC',
        'validity': '', 'scraped_at': '2026-10-21 10:00:00',
        **changes,
    }


class FlightOccurrenceTests(unittest.TestCase):
    def test_eastbound_dateline_flight_is_not_extended_by_an_extra_day(self):
        row = flight(
            departure_time='06:05', arrival_time='21:56',
            origin_timezone='UTC + 09:00', destination_timezone='UTC - 08:00',
        )
        departure, arrival = flight_occurrence(row, date(2026, 10, 21))
        self.assertEqual(departure.isoformat(), '2026-10-20T21:05:00+00:00')
        self.assertEqual(arrival.isoformat(), '2026-10-21T05:56:00+00:00')
        self.assertEqual(arrival - departure, timedelta(hours=8, minutes=51))
        self.assertIsNotNone(active_flight_occurrence(
            row, datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc),
        ))
        self.assertIsNone(active_flight_occurrence(
            row, datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc),
        ))

    def test_westbound_dateline_flight_keeps_correct_duration(self):
        row = flight(
            departure_time='00:00', arrival_time='02:37',
            origin_timezone='UTC - 08:00', destination_timezone='UTC + 09:00',
        )
        departure, arrival = flight_occurrence(row, date(2026, 10, 21))
        self.assertEqual(arrival - departure, timedelta(hours=9, minutes=37))
        self.assertEqual(arrival.isoformat(), '2026-10-21T17:37:00+00:00')

    def test_departure_is_included_but_exact_arrival_is_not_active(self):
        row = flight()
        self.assertIsNotNone(active_flight_occurrence(
            row, datetime(2026, 10, 21, 10, tzinfo=timezone.utc),
        ))
        self.assertIsNone(active_flight_occurrence(
            row, datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
        ))

    def test_future_validity_excludes_not_yet_operating_flight(self):
        row = flight(validity='from 23Oct')
        self.assertIsNone(active_flight_occurrence(
            row, datetime(2026, 10, 21, 11, tzinfo=timezone.utc),
        ))
        self.assertIsNotNone(active_flight_occurrence(
            row, datetime(2026, 10, 23, 11, tzinfo=timezone.utc),
        ))
        self.assertEqual(next_flight_occurrence(
            row, datetime(2026, 10, 21, 11, tzinfo=timezone.utc), 'departure',
        ), datetime(2026, 10, 23, 10, tzinfo=timezone.utc))

    def test_validity_uses_local_departure_date_not_utc_date(self):
        row = flight(
            validity='from 23Oct', departure_time='00:15', arrival_time='02:15',
            origin_timezone='UTC + 14:00', destination_timezone='UTC + 14:00',
        )
        self.assertIsNotNone(active_flight_occurrence(
            row, datetime(2026, 10, 22, 11, tzinfo=timezone.utc),
        ))

    def test_last_valid_departure_can_arrive_after_validity_end(self):
        row = flight(validity='until 21Oct', departure_time='23:00', arrival_time='01:00')
        self.assertIsNotNone(active_flight_occurrence(
            row, datetime(2026, 10, 22, 0, 30, tzinfo=timezone.utc),
        ))
        self.assertIsNone(active_flight_occurrence(
            row, datetime(2026, 10, 22, 23, 30, tzinfo=timezone.utc),
        ))

    def test_yearless_validity_across_new_year_uses_snapshot_date(self):
        row = flight(validity='from 31Dec until 02Jan', scraped_at='2026-12-30 10:00:00')
        for day in (date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 2)):
            self.assertTrue(flight_valid_on(row, day))
        for day in (date(2026, 12, 30), date(2027, 1, 3)):
            self.assertFalse(flight_valid_on(row, day))

    def test_explicit_year_is_preserved(self):
        row = flight(validity='from 01Jan2027 until 31Dec2027')
        self.assertFalse(flight_valid_on(row, date(2026, 10, 21)))
        self.assertTrue(flight_valid_on(row, date(2027, 6, 1)))

    def test_yearless_end_aligns_with_explicit_start_year(self):
        row = flight(validity='from 01Jan2027 until 31Dec')
        self.assertTrue(flight_valid_on(row, date(2027, 12, 31)))
        self.assertFalse(flight_valid_on(row, date(2028, 6, 1)))

    def test_unknown_or_impossible_validity_is_not_silently_ignored(self):
        for validity in ('unknown', 'from 35Oct', 'from 23Foo', 'from 23Oct2026 until 21Oct2026'):
            with self.subTest(validity=validity):
                with self.assertRaises(ValueError):
                    flight_valid_on(flight(validity=validity), date(2026, 10, 21))


if __name__ == '__main__':
    unittest.main()

import random
import unittest

from flightplan_planner import WEEK_MINUTES, generate_weekly_plan, week_time


class WeeklyPlannerTests(unittest.TestCase):
    def plan(self, **changes):
        values = {
            'base': 'FRA', 'start_day': '0', 'start_time': '06:00', 'ground_minutes': '45',
            'routes': [{'destination': 'LHR', 'outbound': '90', 'inbound': '100', 'frequency': '7'}],
        }
        values.update(changes)
        return generate_weekly_plan(**values)

    def assert_consistent(self, plan):
        flights = plan['flights']
        self.assertEqual(len(flights) % 2, 0)
        for index, flight in enumerate(flights):
            self.assertEqual(flight['arrival'] - flight['departure'], flight['duration'])
            self.assertLess(flight['arrival'], flight['ready'])
            self.assertLessEqual(flight['ready'], WEEK_MINUTES)
            if index:
                self.assertGreaterEqual(flight['departure'], flights[index - 1]['ready'])
                self.assertEqual(flight['origin'], flights[index - 1]['destination'])
            if index % 2 == 0:
                self.assertEqual(flight['origin'], plan['base'])
            else:
                self.assertEqual(flight['destination'], plan['base'])
        self.assertEqual(sum(len(day['flights']) for day in plan['days']), len(flights))
        summary = plan['summary']
        self.assertEqual(summary['flight_minutes'] + summary['ground_minutes']
                         + summary['idle_minutes'], summary['available_minutes'])
        for gap in plan['idle']:
            self.assertGreater(gap['minutes'], 0)
            self.assertEqual(gap['end'] - gap['start'], gap['minutes'])
            for flight in flights:
                self.assertTrue(gap['end'] <= flight['departure'] or gap['start'] >= flight['ready'])

    def test_fulfills_frequency_and_uses_asymmetric_times(self):
        plan = self.plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(len(plan['flights']), 14)
        self.assertEqual(plan['flights'][0]['departure_label'], 'Mo 06:00')
        self.assertEqual(plan['flights'][0]['arrival_label'], 'Mo 07:30')
        self.assertEqual(plan['flights'][1]['departure_label'], 'Mo 08:15')
        self.assertEqual(plan['flights'][1]['duration'], 100)
        self.assertEqual(plan['summary']['flight_minutes'], 7 * 190)
        self.assertEqual(plan['summary']['ground_minutes'], 14 * 45)
        self.assert_consistent(plan)

    def test_interleaves_destinations_and_distributes_free_time(self):
        plan = self.plan(routes=[
            {'destination': 'LHR', 'outbound': '90', 'inbound': '100', 'frequency': '7'},
            {'destination': 'CDG', 'outbound': '60', 'inbound': '60', 'frequency': '2'},
        ])
        destinations = [flight['destination'] for flight in plan['flights'][::2]]
        self.assertEqual(destinations.count('LHR'), 7)
        self.assertEqual(destinations.count('CDG'), 2)
        self.assertEqual(destinations[:2], ['LHR', 'CDG'])
        self.assertGreater(plan['flights'][-1]['departure'], 5 * 1440)
        self.assert_consistent(plan)

    def test_midnight_crossing_keeps_correct_days(self):
        plan = self.plan(start_time='23:30', routes=[
            {'destination': 'LHR', 'outbound': '120', 'inbound': '90', 'frequency': '1'},
        ])
        self.assertEqual(plan['flights'][0]['arrival_label'], 'Di 01:30')
        self.assertEqual(plan['flights'][1]['departure_label'], 'Di 02:15')
        self.assertEqual(len(plan['days'][0]['flights']), 1)
        self.assertEqual(len(plan['days'][1]['flights']), 1)
        self.assert_consistent(plan)

    def test_final_base_ground_time_can_end_exactly_at_week_boundary(self):
        plan = self.plan(start_day='6', start_time='21:00', ground_minutes='30', routes=[
            {'destination': 'LHR', 'outbound': '60', 'inbound': '60', 'frequency': '1'},
        ])
        self.assertTrue(plan['complete'])
        self.assertEqual(plan['flights'][-1]['ready'], WEEK_MINUTES)
        self.assertEqual(plan['summary']['idle_minutes'], 0)
        self.assertEqual(plan['summary']['ready_label'], 'Mo (Folgewoche) 00:00')
        self.assert_consistent(plan)

    def test_one_minute_short_does_not_leave_aircraft_away_from_base(self):
        plan = self.plan(start_day='6', start_time='21:01', ground_minutes='30', routes=[
            {'destination': 'LHR', 'outbound': '60', 'inbound': '60', 'frequency': '1'},
        ])
        self.assertFalse(plan['complete'])
        self.assertEqual(plan['flights'], [])
        self.assertEqual(plan['summary']['shortfall_minutes'], 1)
        self.assertIsNone(plan['summary']['ready_label'])
        self.assertEqual(plan['unplanned'], [{'destination': 'LHR', 'count': 1}])
        self.assert_consistent(plan)

    def test_over_capacity_reports_missing_rotations_without_overlaps(self):
        plan = self.plan(routes=[
            {'destination': 'LHR', 'outbound': '600', 'inbound': '600', 'frequency': '28'},
            {'destination': 'CDG', 'outbound': '60', 'inbound': '60', 'frequency': '1'},
        ])
        self.assertFalse(plan['complete'])
        self.assertGreater(plan['summary']['shortfall_minutes'], 0)
        self.assertEqual(plan['summary']['rotations'] + sum(row['count'] for row in plan['unplanned']), 29)
        self.assert_consistent(plan)

    def test_airport_codes_are_normalized(self):
        plan = self.plan(base=' fra ', routes=[
            {'destination': ' lhr ', 'outbound': '90', 'inbound': '90', 'frequency': '1'},
        ])
        self.assertEqual(plan['base'], 'FRA')
        self.assertEqual(plan['flights'][0]['destination'], 'LHR')

    def test_invalid_settings_are_rejected(self):
        for field, bad_values in {
            'base': ('', 'FR', 'FRAA', '<x>', None),
            'start_day': ('-1', '7', '1.5', '', None),
            'start_time': ('24:00', '12:60', '1:00', '', None),
            'ground_minutes': ('0', '721', '1.5', '-1', '999999999999', None),
            'routes': ([], None, [None]),
        }.items():
            for value in bad_values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.plan(**{field: value})

    def test_invalid_route_values_are_rejected(self):
        for field, bad_values in {
            'destination': ('FRA', '', 'LONG', '<script>'),
            'outbound': ('0', '2881', '-2', '1.1', ''),
            'inbound': ('0', '2881', ''),
            'frequency': ('0', '29', '1.5', ''),
        }.items():
            for value in bad_values:
                route = {'destination': 'LHR', 'outbound': '90', 'inbound': '100', 'frequency': '7'}
                route[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.plan(routes=[route])

    def test_duplicate_and_excessive_routes_are_rejected(self):
        route = {'destination': 'LHR', 'outbound': '90', 'inbound': '100', 'frequency': '7'}
        for routes in ([route, {**route, 'destination': 'lhr'}], [route] * 21):
            with self.assertRaises(ValueError):
                self.plan(routes=routes)

    def test_many_combinations_preserve_time_and_location_invariants(self):
        rng = random.Random(42)
        for _ in range(100):
            routes = [
                {'destination': code, 'outbound': str(rng.randint(1, 800)),
                 'inbound': str(rng.randint(1, 800)), 'frequency': str(rng.randint(1, 14))}
                for code in ('LHR', 'CDG', 'AMS')
            ]
            plan = self.plan(start_day=str(rng.randint(0, 6)), ground_minutes=str(rng.randint(1, 180)),
                             routes=routes)
            self.assert_consistent(plan)
            expected = sum(int(route['frequency']) for route in routes)
            self.assertEqual(plan['summary']['rotations'] + sum(row['count'] for row in plan['unplanned']),
                             expected)
            self.assertEqual(plan['complete'], plan['summary']['required_minutes']
                             <= plan['summary']['available_minutes'])

    def test_week_time_labels(self):
        self.assertEqual(week_time(0), 'Mo 00:00')
        self.assertEqual(week_time(10079), 'So 23:59')
        self.assertEqual(week_time(10080), 'Mo (Folgewoche) 00:00')

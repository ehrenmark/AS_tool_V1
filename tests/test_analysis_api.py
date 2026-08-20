import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import app, ensure_analysis_indexes


class AnalysisApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / 'analysis.sqlite3'
        connection = sqlite3.connect(self.database_path)
        connection.executescript("""
            CREATE TABLE airports (
                airport_id INTEGER PRIMARY KEY, iata_code TEXT, icao_code TEXT,
                country TEXT, latitude REAL, longitude REAL
            );
            CREATE TABLE flightplan_flights (
                enterprise_id INTEGER NOT NULL, enterprise_name TEXT, flight_number TEXT,
                frequency TEXT, departure_time TEXT, arrival_time TEXT, aircraft_type TEXT,
                origin_airport_id INTEGER, origin_iata TEXT, origin_name TEXT,
                destination_airport_id INTEGER, destination_iata TEXT, destination_name TEXT
            );
            INSERT INTO airports VALUES
                (1, 'AAA', 'AAAA', 'A', 10, 170),
                (2, 'BBB', 'BBBB', 'B', 10, -170),
                (3, 'CCC', 'CCCC', 'C', 30, 20);
            INSERT INTO flightplan_flights VALUES
                (1, 'One Air', 'O1', '1', '23:00', '01:00', 'A320', 1, 'AAA', 'Alpha', 2, 'BBB', 'Beta'),
                (2, 'Two Air', 'T1', '1', '10:00', '12:00', 'B737', 1, 'AAA', 'Alpha', 2, 'BBB', 'Beta'),
                (1, 'One Air', 'O2', '2', '10:00', '12:00', 'A320', 2, 'BBB', 'Beta', 3, 'CCC', 'Gamma'),
                (2, 'Two Air', 'T2', '1', '09:00', '11:00', 'E190', 3, 'CCC', 'Gamma', 1, 'AAA', 'Alpha');
        """)
        connection.commit()
        connection.close()
        self.path_patch = patch('app.DATABASE_PATH', self.database_path)
        self.path_patch.start()
        self.client = app.test_client()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_plural_parsing_repeated_comma_and_comparison(self):
        response = self.client.get('/api/flightplan/routes?enterprise_ids=1,2&enterprise_ids=2&weekday=1&time=10:30')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['metadata']['enterprise_ids'], [1, 2])
        classes = {feature['properties']['comparison_class'] for feature in body['features']}
        self.assertEqual(classes, {'shared', 'secondary_only'})
        shared = next(feature for feature in body['features'] if feature['properties']['comparison_class'] == 'shared')
        self.assertGreater(len(shared['geometry']['coordinates']), 2)
        self.assertNotIn('enterprise_id', shared['properties'])

    def test_invalid_common_filters_are_stable(self):
        queries = [
            'enterprise_id=1&enterprise_ids=2', 'enterprise_ids=1,2,3',
            'enterprise_ids=0', 'weekday=0', 'weekday=8', 'weekday=x',
            'time=1:00', 'time=24:00',
            'enterprise_id=999999999999999999999999999999',
        ]
        for query in queries:
            with self.subTest(query=query):
                response = self.client.get('/api/analysis/kpis?' + query)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(list(response.get_json()), ['error'])

    def test_weekday_filters_routes_and_analytics_but_time_does_not(self):
        routes = self.client.get('/api/flightplan/routes?weekday=2&time=23:59').get_json()
        self.assertEqual(len(routes['features']), 1)
        kpis = self.client.get('/api/analysis/kpis?weekday=2&time=00:01').get_json()
        self.assertEqual(kpis['data']['flights'], 1)
        self.assertEqual(kpis['data']['routes'], 1)

    def test_aircraft_fixed_instant_includes_prior_day_overnight(self):
        body = self.client.get('/api/flightplan/aircraft?weekday=2&time=00:30').get_json()
        self.assertEqual([feature['properties']['flight_number'] for feature in body['features']], ['O1'])
        self.assertEqual(body['metadata']['instant'], '00:30')
        self.assertAlmostEqual(body['features'][0]['properties']['progress'], .75)

    def test_aircraft_fixed_time_without_weekday_uses_current_utc_day(self):
        class Tuesday(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 4, 8, 0, tzinfo=timezone.utc)

        with patch('app.datetime', Tuesday):
            body = self.client.get('/api/flightplan/aircraft?time=00:30').get_json()

        self.assertEqual([feature['properties']['flight_number'] for feature in body['features']], ['O1'])
        self.assertIsNone(body['metadata']['weekday'])
        self.assertEqual(body['metadata']['instant'], '00:30')

    def test_aircraft_without_time_uses_current_utc_day_and_time(self):
        class Tuesday(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 4, 11, 0, tzinfo=timezone.utc)

        with patch('app.datetime', Tuesday):
            body = self.client.get('/api/flightplan/aircraft').get_json()

        self.assertEqual([feature['properties']['flight_number'] for feature in body['features']], ['O2'])
        self.assertEqual(body['metadata']['instant'], '11:00')

    def test_aircraft_metadata_is_stable_without_database(self):
        with patch('app.DATABASE_PATH', self.database_path.with_name('missing.sqlite3')):
            body = self.client.get('/api/flightplan/aircraft?weekday=2&time=00:30').get_json()
        self.assertEqual(body['features'], [])
        self.assertEqual(body['metadata']['instant'], '00:30')
        self.assertEqual(body['metadata']['aircraft_count'], 0)

    def test_unselected_routes_are_aggregated_directed_and_great_circle(self):
        body = self.client.get('/api/flightplan/routes?weekday=1').get_json()
        self.assertEqual(len(body['features']), 2)
        shared = next(feature for feature in body['features'] if feature['properties']['flight_count'] == 2)
        self.assertGreater(len(shared['geometry']['coordinates']), 2)
        self.assertLess(abs(shared['geometry']['coordinates'][1][0] - shared['geometry']['coordinates'][0][0]), 180)
        self.assertEqual(shared['properties']['airline_count'], 2)
        self.assertNotIn('enterprise_ids', shared['properties'])
        self.assertNotIn('airlines', shared['properties'])
        self.assertEqual(body['metadata']['geometry_detail'], 'great_circle')

    def test_all_airlines_aircraft_uses_returned_dateline_route_geometry(self):
        routes = self.client.get('/api/flightplan/routes?weekday=1').get_json()['features']
        route = next(feature for feature in routes
                     if feature['properties']['origin_airport_id'] == 1
                     and feature['properties']['destination_airport_id'] == 2)
        coordinates = route['geometry']['coordinates']
        aircraft = self.client.get(
            '/api/flightplan/aircraft?weekday=1&time=11:00').get_json()['features']
        position = next(feature['geometry']['coordinates'] for feature in aircraft
                        if feature['properties']['flight_number'] == 'T1')

        self.assertEqual(len(coordinates), 11)
        self.assertAlmostEqual(position[0], coordinates[5][0])
        self.assertAlmostEqual(position[1], coordinates[5][1])
        self.assertLess(max(abs(end[0] - start[0])
                            for start, end in zip(coordinates, coordinates[1:])), 180)

    def test_all_airlines_long_route_and_aircraft_share_interpolation(self):
        connection = sqlite3.connect(self.database_path)
        connection.executescript("""
            INSERT INTO airports VALUES (4, 'DDD', 'DDDD', 'D', 40.7, -74);
            INSERT INTO airports VALUES (5, 'EEE', 'EEEE', 'E', 48.85, 2.35);
            INSERT INTO flightplan_flights VALUES
                (3, 'Long Air', 'L1', '1', '08:00', '12:00', 'A330',
                 4, 'DDD', 'Delta', 5, 'EEE', 'Echo');
        """)
        connection.commit()
        connection.close()

        route = next(feature for feature in
                     self.client.get('/api/flightplan/routes?weekday=1').get_json()['features']
                     if feature['properties']['origin_airport_id'] == 4)
        coordinates = route['geometry']['coordinates']
        aircraft = next(feature for feature in self.client.get(
            '/api/flightplan/aircraft?weekday=1&time=10:00').get_json()['features']
                        if feature['properties']['flight_number'] == 'L1')
        scaled = .5 * (len(coordinates) - 1)
        index = math.floor(scaled)
        midpoint = [(coordinates[index][axis] + coordinates[index + 1][axis]) / 2
                    for axis in range(2)]

        self.assertGreater(len(coordinates), 20)
        self.assertGreater(max(point[1] for point in coordinates), 50)
        self.assertAlmostEqual(aircraft['geometry']['coordinates'][0], midpoint[0])
        self.assertAlmostEqual(aircraft['geometry']['coordinates'][1], midpoint[1])

    def test_single_selection_preserves_legacy_properties(self):
        feature = self.client.get('/api/flightplan/routes?enterprise_id=1&weekday=1').get_json()['features'][0]
        self.assertEqual(feature['properties']['enterprise_id'], 1)
        self.assertEqual(feature['properties']['enterprise_name'], 'One Air')

    def test_kpis_destinations_and_airport_detail(self):
        kpis = self.client.get('/api/flightplan/kpis?enterprise_ids=1,2&weekday=1').get_json()['data']
        self.assertEqual((kpis['flights'], kpis['routes'], kpis['destinations']), (3, 2, 2))
        self.assertEqual(kpis['comparison'], {'primary_only': 0, 'secondary_only': 1, 'shared': 1})
        self.assertEqual(kpis['longest_routes'][0]['origin_iata'], 'CCC')
        self.assertEqual(kpis['aircraft_type_counts']['A320'], 1)
        destinations = self.client.get('/api/flightplan/destinations?weekday=1').get_json()['destinations']
        self.assertEqual(destinations[0]['flight_count'], 2)
        self.assertEqual(len(destinations[0]['airlines']), 2)
        detail = self.client.get('/api/airports/1/flightplan?weekday=1').get_json()
        self.assertEqual(detail['airport']['iata_code'], 'AAA')
        self.assertEqual(detail['airport']['country'], 'A')
        self.assertEqual((len(detail['departing']), len(detail['arriving'])), (2, 1))
        self.assertEqual(detail['strongest_connections'][0]['iata'], 'BBB')
        self.assertEqual(len(detail['airlines']), 2)
        self.assertEqual(self.client.get('/api/airports/999/flightplan').status_code, 404)

    def test_airport_detail_returns_next_ten_arrivals_and_departures(self):
        connection = sqlite3.connect(self.database_path)
        rows = []
        for index in range(12):
            minute = index * 5
            departure = f'{9 + minute // 60:02d}:{minute % 60:02d}'
            arrival_minute = minute + 30
            arrival = f'{9 + arrival_minute // 60:02d}:{arrival_minute % 60:02d}'
            rows.append((3, 'Many Air', f'D{index:02d}', '1', departure, arrival, 'A220',
                         1, 'AAA', 'Alpha', 2, 'BBB', 'Beta'))
            rows.append((3, 'Many Air', f'A{index:02d}', '1', departure, arrival, 'A220',
                         2, 'BBB', 'Beta', 1, 'AAA', 'Alpha'))
        connection.executemany('INSERT INTO flightplan_flights VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        connection.commit()
        connection.close()

        class Monday(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 3, 8, 30, tzinfo=timezone.utc)

        with patch('app.datetime', Monday):
            detail = self.client.get('/api/airports/1/flightplan?enterprise_id=3').get_json()

        self.assertEqual([flight['flight_number'] for flight in detail['departing']],
                         [f'D{index:02d}' for index in range(10)])
        self.assertEqual([flight['flight_number'] for flight in detail['arriving']],
                         [f'A{index:02d}' for index in range(10)])
        self.assertEqual(detail['departing'][0]['next_departure_at'], '2026-08-03T09:00:00+00:00')
        self.assertEqual(detail['arriving'][0]['next_arrival_at'], '2026-08-03T09:30:00+00:00')

    def test_airport_detail_includes_active_overnight_as_next_arrival(self):
        class Tuesday(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 4, 0, 30, tzinfo=timezone.utc)

        with patch('app.datetime', Tuesday):
            detail = self.client.get('/api/airports/2/flightplan?enterprise_id=1').get_json()

        self.assertEqual(detail['arriving'][0]['flight_number'], 'O1')
        self.assertEqual(detail['arriving'][0]['next_arrival_at'], '2026-08-04T01:00:00+00:00')

    def test_longest_routes_combine_both_directions(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute('''
            INSERT INTO flightplan_flights VALUES
            (1, 'One Air', 'O3', '1', '13:00', '15:00', 'A320',
             2, 'BBB', 'Beta', 1, 'AAA', 'Alpha')
        ''')
        connection.commit()
        connection.close()

        data = self.client.get('/api/flightplan/kpis?weekday=1').get_json()['data']
        matching = [route for route in data['longest_routes']
                    if {route['origin_airport_id'], route['destination_airport_id']} == {1, 2}]

        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0]['bidirectional'])

    def test_import_status_absent_then_present_with_optional_columns(self):
        absent = self.client.get('/api/flightplan/import-status?enterprise_ids=1,2').get_json()
        self.assertFalse(absent['available'])
        connection = sqlite3.connect(self.database_path)
        connection.executescript("""
            CREATE TABLE import_runs (id INTEGER PRIMARY KEY, started_at TEXT, status TEXT);
            INSERT INTO import_runs VALUES (1, '2026-01-01T00:00:00+00:00', 'success');
        """)
        connection.commit()
        connection.close()
        present = self.client.get('/api/analysis/import-status?weekday=1').get_json()
        self.assertTrue(present['available'])
        self.assertEqual(present['latest']['status'], 'success')

    def test_indexes_are_explicit_idempotent_and_schema_aware(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        for _ in range(2):
            self.assertEqual(len(ensure_analysis_indexes(connection)), 3)
            connection.commit()
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_flightplan_%'")}
        connection.close()
        self.assertEqual(len(names), 3)

    def test_nullable_enterprise_ids_are_ignored(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute('ALTER TABLE flightplan_flights RENAME TO old_flights')
            connection.execute('CREATE TABLE flightplan_flights AS SELECT * FROM old_flights')
            connection.execute('''
                INSERT INTO flightplan_flights
                (enterprise_id, origin_airport_id, destination_airport_id, frequency)
                VALUES (NULL, 1, 2, '1')
            ''')
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.client.get('/api/analysis/kpis?weekday=1').get_json()['data']['flights'], 3)
        self.assertEqual(len(self.client.get('/api/flightplan/routes?weekday=1').get_json()['features']), 2)

    def test_index_name_collision_uses_verified_fallback(self):
        connection = sqlite3.connect(self.database_path)
        connection.executescript('''
            CREATE TABLE unrelated (enterprise_id INTEGER);
            CREATE INDEX idx_flightplan_enterprise ON unrelated (enterprise_id);
        ''')
        connection.commit()
        connection.close()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        names = ensure_analysis_indexes(connection)
        connection.commit()
        connection.close()
        self.assertIn('idx_flightplan_enterprise_analysis2', names)
        connection = sqlite3.connect(self.database_path)
        owner = connection.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            ('idx_flightplan_enterprise_analysis2',),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(owner, 'flightplan_flights')

    def test_minimal_schema_does_not_break_analysis(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute('DROP TABLE flightplan_flights')
        connection.execute('CREATE TABLE flightplan_flights (enterprise_id INTEGER)')
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get('/api/analysis/kpis').get_json()['data']['flights'], 0)
        self.assertEqual(self.client.get('/api/flightplan/airlines').get_json(), [])
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        self.assertEqual(ensure_analysis_indexes(connection), ['idx_flightplan_enterprise'])
        connection.commit()
        connection.close()

        connection = sqlite3.connect(self.database_path)
        connection.execute('INSERT INTO flightplan_flights VALUES (7)')
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get('/api/flightplan/airlines').get_json(), [{
            'enterprise_id': 7, 'enterprise_name': 'Airline 7', 'flight_count': 1,
        }])

    def test_missing_optional_airport_coordinates_do_not_break_analytics(self):
        connection = sqlite3.connect(self.database_path)
        connection.executescript('''
            DROP TABLE airports;
            CREATE TABLE airports (airport_id INTEGER PRIMARY KEY);
            INSERT INTO airports VALUES (1), (2), (3);
        ''')
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get('/api/analysis/kpis').get_json()['data']['flights'], 4)
        self.assertEqual(self.client.get('/api/flightplan/routes').get_json()['features'], [])

    def test_same_day_overnight_does_not_appear_before_departure(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("UPDATE flightplan_flights SET frequency='2' WHERE flight_number='O1'")
        connection.commit()
        connection.close()
        features = self.client.get('/api/flightplan/aircraft?weekday=2&time=00:30').get_json()['features']
        self.assertNotIn('O1', [feature['properties']['flight_number'] for feature in features])

    def test_analytics_include_flights_with_missing_airport_rows(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute('DELETE FROM airports WHERE airport_id=3')
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get('/api/flightplan/kpis').get_json()['data']['flights'], 4)
        self.assertEqual(len(self.client.get('/api/flightplan/routes').get_json()['features']), 1)


if __name__ == '__main__':
    unittest.main()

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app


class AirlineApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / 'test.sqlite3'
        connection = sqlite3.connect(self.database_path)
        connection.executescript(
            """
            CREATE TABLE airports (
                airport_id INTEGER PRIMARY KEY,
                iata_code TEXT,
                icao_code TEXT,
                country TEXT,
                airport_size INTEGER,
                passenger_demand INTEGER,
                cargo_demand INTEGER,
                latitude REAL,
                longitude REAL
            );
            CREATE TABLE flightplan_flights (
                enterprise_id INTEGER NOT NULL,
                enterprise_name TEXT,
                flight_number TEXT,
                frequency TEXT,
                departure_time TEXT,
                arrival_time TEXT,
                aircraft_type TEXT,
                origin_airport_id INTEGER,
                origin_iata TEXT,
                origin_name TEXT,
                destination_airport_id INTEGER,
                destination_iata TEXT,
                destination_name TEXT
            );
            INSERT INTO airports VALUES
                (1, 'AAA', 'AAAA', 'A', 1, 1, 1, 0, 0),
                (2, 'BBB', 'BBBB', 'B', 1, 1, 1, 10, 10);
            INSERT INTO flightplan_flights VALUES
                (2, 'Zulu Air', 'ZU1', '1234567', '00:00', '23:59', 'A320', 1, 'AAA', 'Alpha', 2, 'BBB', 'Beta'),
                (1, 'Alpha Air', 'AL1', '1234567', '00:00', '23:59', 'B737', 2, 'BBB', 'Beta', 1, 'AAA', 'Alpha');
            """
        )
        connection.commit()
        connection.close()
        self.path_patch = patch('app.DATABASE_PATH', self.database_path)
        self.path_patch.start()
        self.client = app.test_client()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_airlines_are_listed_by_name(self):
        response = self.client.get('/api/flightplan/airlines')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['enterprise_name'] for item in response.get_json()], ['Alpha Air', 'Zulu Air'])

    def test_routes_can_be_filtered_by_enterprise(self):
        response = self.client.get('/api/flightplan/routes?enterprise_id=1')

        self.assertEqual(response.status_code, 200)
        features = response.get_json()['features']
        self.assertEqual(len(features), 1)
        self.assertEqual(features[0]['properties']['enterprise_id'], 1)

    def test_invalid_enterprise_filter_is_rejected(self):
        response = self.client.get('/api/flightplan/routes?enterprise_id=invalid')

        self.assertEqual(response.status_code, 400)
        self.assertIn('enterprise_id', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()

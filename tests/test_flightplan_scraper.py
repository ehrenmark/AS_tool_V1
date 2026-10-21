import sqlite3
import unittest

from jobs.scrape_flightplan import (
    ensure_table,
    parse_directory_letters,
    parse_enterprise_directory,
    parse_flightplan,
    store_flightplan,
)


class FlightplanScraperTests(unittest.TestCase):
    def test_via_flight_number_keeps_summary_and_both_legs(self):
        connection = sqlite3.connect(':memory:')
        ensure_table(connection)
        base = {
            'enterprise_id': 74, 'enterprise_name': 'Hummingbird',
            'flight_number': 'HMB 1365', 'frequency': '1234567',
            'aircraft_type': '351', 'operator': '', 'validity': '',
            'source_url': 'https://example.test/flightplan',
        }
        flights = [
            {**base, 'departure_time': '18:40', 'arrival_time': '01:15', 'remarks': '',
             'origin_airport_id': 1, 'origin_iata': 'LAX', 'origin_name': 'Los Angeles',
             'destination_airport_id': 2, 'destination_iata': 'ATL', 'destination_name': 'Atlanta'},
            {**base, 'departure_time': '05:20', 'arrival_time': '06:52', 'remarks': '',
             'origin_airport_id': 2, 'origin_iata': 'ATL', 'origin_name': 'Atlanta',
             'destination_airport_id': 3, 'destination_iata': 'MIA', 'destination_name': 'Miami'},
            {**base, 'departure_time': '21:40', 'arrival_time': '06:52', 'remarks': 'via ATL',
             'origin_airport_id': 1, 'origin_iata': 'LAX', 'origin_name': 'Los Angeles',
             'destination_airport_id': 3, 'destination_iata': 'MIA', 'destination_name': 'Miami'},
        ]

        store_flightplan(connection, 74, flights, replace=True)

        rows = connection.execute(
            'SELECT flight_number, origin_iata, destination_iata, remarks '
            'FROM flightplan_flights ORDER BY origin_iata, destination_iata'
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row[0] for row in rows}, {'HMB 1365'})
        self.assertIn(('HMB 1365', 'LAX', 'MIA', 'via ATL'), rows)
        connection.close()

    def test_directory_letters_are_discovered(self):
        html = """
            <a href="./enterprises?letter=A">A</a>
            <a href="/app/info/enterprises?letter=Z">Z</a>
            <a href="./enterprises/74">Hummingbird</a>
        """

        self.assertEqual(
            parse_directory_letters('https://example.test/app/info/enterprises', html),
            ['A', 'Z'],
        )

    def test_enterprises_are_parsed_and_deduplicated(self):
        html = """
            <table class="enterprises"><tbody>
                <tr><td></td><td></td><td><a href="./enterprises/74">Hummingbird</a></td></tr>
                <tr><td></td><td></td><td><a href="/app/info/enterprises/12">Alpha Air</a></td></tr>
                <tr><td></td><td></td><td><a href="./enterprises/74">Hummingbird</a></td></tr>
            </tbody></table>
        """

        enterprises = parse_enterprise_directory(
            'https://example.test/app/info/enterprises?letter=A',
            html,
        )

        self.assertEqual(
            enterprises,
            [
                {'enterprise_id': 12, 'enterprise_name': 'Alpha Air'},
                {'enterprise_id': 74, 'enterprise_name': 'Hummingbird'},
            ],
        )

    def test_enterprise_without_flightplan_returns_no_flights(self):
        flights = parse_flightplan(
            12,
            'https://example.test/app/info/enterprises/12?tab=3',
            '<html><title>Alpha Air | AirlineSim</title><h2>Alpha Air</h2>'
            '<h3>Flight schedule</h3><p>No data available.</p></html>',
        )

        self.assertEqual(flights, [])

    def test_unexpected_directory_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Enterprise-Tabelle'):
            parse_enterprise_directory('https://example.test/app/info/enterprises', '<html></html>')

    def test_unrecognized_directory_table_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Keine gültigen Enterprises'):
            parse_enterprise_directory(
                'https://example.test/app/info/enterprises',
                '<table class="enterprises"><tbody><tr><td>changed markup</td></tr></tbody></table>',
            )

    def test_unexpected_flightplan_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unerwartete Flugplan-Seite'):
            parse_flightplan(12, 'https://example.test/app/info/enterprises/12?tab=3', '<html></html>')

    def test_unrecognized_flightplan_table_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Keine gültigen Flugplan-Zeilen'):
            parse_flightplan(
                12,
                'https://example.test/app/info/enterprises/12?tab=3',
                '<html><table><tr><td>login or changed markup</td></tr></table></html>',
            )

    def test_failed_replacement_rolls_back_deletion(self):
        connection = sqlite3.connect(':memory:')
        connection.execute(
            'CREATE TABLE flightplan_flights (enterprise_id INTEGER, flight_number TEXT)'
        )
        connection.execute('INSERT INTO flightplan_flights VALUES (12, "OLD")')
        connection.commit()

        with self.assertRaises(ValueError):
            store_flightplan(
                connection,
                12,
                [{'enterprise_id': 12, 'flight_number': 'NEW'}],
                replace=True,
            )

        self.assertEqual(
            connection.execute('SELECT enterprise_id, flight_number FROM flightplan_flights').fetchall(),
            [(12, 'OLD')],
        )
        connection.close()

    def test_lost_lease_rolls_back_replacement(self):
        connection = sqlite3.connect(':memory:')
        connection.execute(
            'CREATE TABLE flightplan_flights (enterprise_id INTEGER, flight_number TEXT)'
        )
        connection.execute('INSERT INTO flightplan_flights VALUES (12, "OLD")')
        connection.commit()

        with self.assertRaisesRegex(RuntimeError, 'lease lost'):
            store_flightplan(
                connection,
                12,
                [{'enterprise_id': 12, 'flight_number': 'NEW'}],
                replace=True,
                transaction_guard=lambda current: (_ for _ in ()).throw(
                    RuntimeError('lease lost')
                ),
            )

        self.assertEqual(
            connection.execute('SELECT enterprise_id, flight_number FROM flightplan_flights').fetchall(),
            [(12, 'OLD')],
        )
        connection.close()


if __name__ == '__main__':
    unittest.main()

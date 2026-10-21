import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jobs.scrape_flightplan import (
    DEFAULT_BASE_URL,
    FlightplanSnapshot,
    connect_to_sqlite,
    ensure_table,
    parse_flightplan,
    parse_enterprise_directory,
    scrape_enterprises,
    store_directory,
    store_flightplan,
)


def flight(number="AA 1", **changes):
    return {
        "enterprise_id": 1, "enterprise_name": "Alpha Air", "flight_number": number,
        "frequency": "1234567", "departure_time": "10:00", "arrival_time": "12:00",
        "aircraft_type": "320", "operator": "Alpha Air", "remarks": "", "validity": "",
        "origin_airport_id": 1, "origin_iata": "AAA", "origin_name": "Alpha",
        "destination_airport_id": 2, "destination_iata": "BBB", "destination_name": "Beta",
        "source_url": DEFAULT_BASE_URL + "/app/info/enterprises/1?tab=3",
        **changes,
    }


class FlightplanSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "test.sqlite3"
        self.connection = connect_to_sqlite(self.db_path)
        ensure_table(self.connection)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def rows(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM flightplan_flights ORDER BY id"
        )]

    def airline(self, enterprise_id=1):
        return self.connection.execute(
            "SELECT * FROM airlines WHERE enterprise_id=?", (enterprise_id,)
        ).fetchone()

    def test_snapshot_inserts_updates_deletes_and_preserves_unchanged_ids(self):
        store_flightplan(self.connection, 1, [flight(), flight("AA 2")])
        original_id = self.rows()[0]["id"]
        store_flightplan(self.connection, 1, [
            flight(aircraft_type="321"), flight("AA 3"),
        ])

        rows = self.rows()
        self.assertEqual([row["flight_number"] for row in rows], ["AA 1", "AA 3"])
        self.assertEqual(rows[0]["id"], original_id)
        self.assertEqual(rows[0]["aircraft_type"], "321")
        self.assertIsNotNone(self.airline()["last_success_at"])

    def test_changed_timing_replaces_old_schedule_instead_of_leaving_stale_row(self):
        store_flightplan(self.connection, 1, [flight()])
        store_flightplan(self.connection, 1, [flight(departure_time="11:00")])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["departure_time"], "11:00")

    def test_update_never_changes_another_airline(self):
        store_flightplan(self.connection, 1, [flight()])
        store_flightplan(self.connection, 2, [flight(enterprise_id=2, enterprise_name="Beta")])
        other = self.rows()[1]
        store_flightplan(self.connection, 1, [])
        self.assertEqual(self.rows(), [other])

    def test_frequency_and_validity_variants_do_not_overwrite_each_other(self):
        schedules = [
            flight(frequency="135"), flight(frequency="246"),
            flight(frequency="135", validity="from 01.11."),
        ]
        store_flightplan(self.connection, 1, schedules)
        store_flightplan(self.connection, 1, schedules)
        self.assertEqual(len(self.rows()), 3)

    def test_via_summary_has_separate_identity_from_direct_flight(self):
        store_flightplan(self.connection, 1, [flight(), flight(remarks="via CCC")])
        self.assertEqual([row["is_via_summary"] for row in self.rows()], [0, 1])

    def test_identical_input_rows_are_deduplicated(self):
        count = store_flightplan(self.connection, 1, [flight(), flight()])
        self.assertEqual(count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_conflicting_input_preserves_last_good_snapshot(self):
        store_flightplan(self.connection, 1, [flight()])
        original = self.rows()
        with self.assertRaisesRegex(ValueError, "Widerspr"):
            store_flightplan(self.connection, 1, [flight(), flight(aircraft_type="321")])
        self.assertEqual(self.rows(), original)

    def test_mixed_airlines_and_invalid_keys_are_rejected(self):
        store_flightplan(self.connection, 1, [flight()])
        original = self.rows()
        for changes in (
            {"enterprise_id": 2}, {"origin_airport_id": None},
            {"destination_airport_id": 0}, {"departure_time": None},
            {"flight_number": ""},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    store_flightplan(self.connection, 1, [flight(**changes)])
                self.assertEqual(self.rows(), original)

    def test_database_failure_rolls_back_updates_and_airline_metadata(self):
        store_flightplan(self.connection, 1, [flight()])
        original = self.rows()
        self.connection.execute("""
            CREATE TRIGGER reject_new BEFORE INSERT ON flightplan_flights
            WHEN NEW.flight_number = 'AA 2'
            BEGIN SELECT RAISE(ABORT, 'test rejection'); END
        """)
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            store_flightplan(self.connection, 1, [
                flight(aircraft_type="321"), flight("AA 2"),
            ], enterprise_name="Renamed airline")
        self.assertEqual(self.rows(), original)
        self.assertEqual(self.airline()["enterprise_name"], "Alpha Air")

    def test_unknown_airline_reference_is_rejected_by_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("""
                INSERT INTO flightplan_flights
                    (enterprise_id, flight_number, departure_time, arrival_time,
                     origin_airport_id, destination_airport_id, source_url)
                VALUES (99, 'AA 1', '10:00', '12:00', 1, 2, 'test')
            """)
        self.connection.rollback()

    def test_invalid_airline_id_does_not_create_an_automatic_id(self):
        for enterprise_id in (None, 0, -1, True):
            with self.subTest(enterprise_id=enterprise_id):
                with self.assertRaises(ValueError):
                    store_flightplan(self.connection, enterprise_id, [])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM airlines").fetchone()[0], 0
        )

    def test_append_is_explicit_and_keeps_absent_flights(self):
        store_flightplan(self.connection, 1, [flight()])
        store_flightplan(self.connection, 1, [flight("AA 2")], replace=False)
        self.assertEqual(len(self.rows()), 2)

    def test_catalog_retains_empty_airlines_and_removes_closed_airline_flights(self):
        store_flightplan(self.connection, 1, [flight()])
        store_directory(self.connection, [
            {"enterprise_id": 2, "enterprise_name": "Beta Air"},
        ], DEFAULT_BASE_URL)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.airline()["is_active"], 0)
        self.assertEqual(self.airline(2)["is_active"], 1)
        with self.assertRaises(ValueError):
            store_directory(self.connection, [], DEFAULT_BASE_URL)
        self.assertEqual(self.airline(2)["is_active"], 1)

    def test_failed_fetch_preserves_flights_and_records_error(self):
        store_flightplan(self.connection, 1, [flight()])
        original = self.rows()
        with patch("jobs.scrape_flightplan.fetch_flightplan", side_effect=ValueError("bad page")):
            result = scrape_enterprises(
                self.db_path, [{"enterprise_id": 1, "enterprise_name": "Alpha Air"}],
                session=Mock(),
            )
        self.assertEqual(result.enterprises_failed, 1)
        self.assertEqual(self.rows(), original)
        self.assertEqual(self.airline()["last_error"], "bad page")
        self.assertIsNotNone(self.airline()["last_success_at"])

    def test_successful_empty_schedule_removes_flights_and_retains_airline(self):
        store_flightplan(self.connection, 1, [flight()])
        with patch("jobs.scrape_flightplan.fetch_flightplan",
                   return_value=FlightplanSnapshot("Alpha renamed", [])):
            result = scrape_enterprises(
                self.db_path, [{"enterprise_id": 1, "enterprise_name": None}],
                session=Mock(),
            )
        self.assertEqual(result.enterprises_succeeded, 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.airline()["enterprise_name"], "Alpha renamed")

    def test_incomplete_page_is_not_treated_as_empty_schedule(self):
        for html in (
            "<title>Alpha Air | AirlineSim</title><h2>Alpha Air</h2><h3>Flight schedule</h3>",
            "<title>Login | AirlineSim</title><h3>Flight schedule</h3>No data available.",
            "<table><tr class='line'><td>AA 1</td></tr></table>",
        ):
            with self.subTest(html=html):
                with self.assertRaises(ValueError):
                    parse_flightplan(1, DEFAULT_BASE_URL, html)

    def test_partially_malformed_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_enterprise_directory(DEFAULT_BASE_URL, """
                <table class="enterprises"><tbody>
                    <tr><td><a href="/app/info/enterprises/1">Alpha Air</a></td></tr>
                    <tr><td>unexpected row without airline link</td></tr>
                </tbody></table>
            """)

    def test_malformed_row_after_valid_row_rejects_entire_page(self):
        html = """
            <title>Alpha Air | AirlineSim</title>
            <table>
              <tr class="origin"><th><span>Alpha</span><a href="../airports/1">AAA</a></th></tr>
              <tr class="destination"><th><span>Beta</span><a href="../airports/2">BBB</a></th></tr>
              <tr class="line"><td>AA 1</td><td>1234567</td><td>10:00</td><td>12:00</td>
                <td>320</td><td>Alpha Air</td><td></td><td></td></tr>
              <tr class="line"><td>AA 2</td></tr>
            </table>
        """
        with self.assertRaisesRegex(ValueError, "Unvollst"):
            parse_flightplan(1, DEFAULT_BASE_URL, html)

    def test_via_marker_survives_nested_html(self):
        html = """
            <title>Alpha Air | AirlineSim</title>
            <table>
              <tr class="origin"><th><span>Alpha</span><a href="../airports/1">AAA</a></th></tr>
              <tr class="destination"><th><span>Beta</span><a href="../airports/2">BBB</a></th></tr>
              <tr class="line"><td>AA 1</td><td>1234567</td><td>10:00</td><td>12:00</td>
                <td>320</td><td>Alpha Air</td><td><span>via</span><a>CCC</a></td><td></td></tr>
            </table>
        """
        rows = parse_flightplan(1, DEFAULT_BASE_URL, html)
        self.assertEqual(rows[0]["remarks"], "via CCC")
        self.assertEqual(rows[0]["is_via_summary"], 1)

    def test_old_schema_is_migrated_preserving_ids_and_backfilling_airlines(self):
        self.connection.close()
        self.db_path.unlink()
        self.connection = connect_to_sqlite(self.db_path)
        self.connection.executescript("""
            CREATE TABLE flightplan_flights (
                id INTEGER PRIMARY KEY AUTOINCREMENT, enterprise_id INTEGER NOT NULL,
                enterprise_name TEXT, flight_number TEXT NOT NULL, frequency TEXT,
                departure_time TEXT, arrival_time TEXT, aircraft_type TEXT,
                operator TEXT, remarks TEXT, validity TEXT, origin_airport_id INTEGER,
                origin_iata TEXT, origin_name TEXT, destination_airport_id INTEGER,
                destination_iata TEXT, destination_name TEXT, source_url TEXT NOT NULL,
                scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(enterprise_id, flight_number, origin_airport_id,
                       destination_airport_id, departure_time, arrival_time)
            );
            INSERT INTO flightplan_flights
                (id, enterprise_id, enterprise_name, flight_number, departure_time,
                 arrival_time, origin_airport_id, destination_airport_id, remarks, source_url)
            VALUES (42, 1, 'Alpha Air', 'AA 1', '10:00', '12:00', 1, 2, 'via CCC',
                    'https://hindenburg.airlinesim.aero/app/info/enterprises/1?tab=3');
        """)
        ensure_table(self.connection)
        ensure_table(self.connection)
        row = self.rows()[0]
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["frequency"], "")
        self.assertEqual(row["validity"], "")
        self.assertEqual(row["is_via_summary"], 1)
        self.assertEqual(self.airline()["enterprise_name"], "Alpha Air")
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()

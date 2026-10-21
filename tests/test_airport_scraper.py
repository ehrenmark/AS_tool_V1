import sqlite3
import unittest
from unittest.mock import Mock

from jobs.scrape_airports import (
    CREATE_TABLE_SQL,
    UPSERT_SQL,
    discover_airport_ids,
    ensure_table,
    parse_airport_page,
)


def airport_page(name="Abu Simbel", prefix="Airport", section="Information"):
    return f"""
        <h1>{prefix}: <span>{name}</span> (ABS)</h1>
        <h3>{section}</h3>
        <div class="as-panel"><table class="table">
            <tr><td>IATA code</td><td>ABS</td></tr>
            <tr><td>ICAO code</td><td>HEBL</td></tr>
            <tr><td>Country</td><td>Egypt</td></tr>
            <tr><td>Slots (per 5 minutes)</td><td>8</td></tr>
            <tr><td>Passengers</td><td><img title="3" /></td></tr>
        </table></div>
        <h1>Modal title</h1>
    """


class AirportScraperTests(unittest.TestCase):
    def test_discovers_all_airports_despite_large_id_gaps(self):
        pages = [
            """
            <table class="countries">
                <a href="country?id=1">Egypt</a>
                <a href="country?id=2">China</a>
                <a href="country?id=1">Egypt again</a>
            </table>
            """,
            '<table><a href="../info/airport?id=1">Abu Simbel</a></table>',
            """
            <a href="/app/info/airports/9999">News about another airport</a>
            <table><a href="../info/airport?id=5000">Qitai</a></table>
            """,
        ]
        session = Mock()
        session.get.side_effect = [Mock(text=html) for html in pages]

        self.assertEqual(discover_airport_ids(session, timeout=20, delay=0), [1, 5000])
        self.assertEqual(session.get.call_count, 3)

    def test_discovery_rejects_missing_directory_and_country_data(self):
        for pages in (
            ["<html>Login</html>"],
            [
                '<table class="countries"><a href="country?id=1">Egypt</a></table>',
                "<html>Login</html>",
            ],
            [
                '<table class="countries"><a href="country?id=1">Egypt</a></table>',
                '<table><a href="../info/airport?id=invalid">Invalid</a></table>',
            ],
        ):
            session = Mock()
            session.get.side_effect = [Mock(text=html) for html in pages]
            with self.subTest(pages=pages):
                with self.assertRaises(ValueError):
                    discover_airport_ids(session, timeout=20, delay=0)

    def test_discovery_follows_regional_airport_lists_once(self):
        session = Mock()
        session.get.side_effect = [
            Mock(text='<table class="countries"><a href="country?id=12">AU</a></table>'),
            Mock(text="""
                <table>
                    <a href="../info/county?id=3">ACT</a>
                    <a href="../info/county?id=3">ACT again</a>
                    <a href="../info/county?id=35">Empty region</a>
                </table>
            """),
            Mock(text="""
                <table><a href="../info/airport?id=161">Canberra</a></table>
            """),
            Mock(text="<h3>Region: Netherlands Antilles</h3>"),
        ]
        self.assertEqual(discover_airport_ids(session, timeout=20, delay=0), [161])
        self.assertEqual(session.get.call_count, 4)

    def test_extracts_name_without_label_or_iata(self):
        airport = parse_airport_page(1, airport_page())

        self.assertEqual(airport["airport_name"], "Abu Simbel")
        self.assertEqual(airport["iata_code"], "ABS")
        self.assertEqual(airport["icao_code"], "HEBL")
        self.assertEqual(airport["country"], "Egypt")
        self.assertEqual(airport["slots_per_5_minutes"], 8)
        self.assertEqual(airport["passenger_demand"], 3)
        self.assertEqual(
            airport["source_url"],
            "https://hindenburg.airlinesim.aero/app/info/airports/1",
        )

    def test_preserves_parentheses_accents_and_normalizes_whitespace(self):
        airport = parse_airport_page(
            1, airport_page("  S\u00e3o   Paulo (International)  ")
        )
        self.assertEqual(airport["airport_name"], "S\u00e3o Paulo (International)")

    def test_supports_german_heading(self):
        airport = parse_airport_page(
            1, airport_page(prefix="Flughafen", section="Informationen")
        )
        self.assertEqual(airport["airport_name"], "Abu Simbel")

    def test_rejects_missing_or_mismatched_name(self):
        for heading in ("<h1>Modal title</h1>", "<h1>Airport: Abu Simbel (XXX)</h1>"):
            html = airport_page().replace(
                "<h1>Airport: <span>Abu Simbel</span> (ABS)</h1>", heading
            )
            with self.subTest(heading=heading):
                with self.assertRaisesRegex(ValueError, "Flughafenname"):
                    parse_airport_page(1, html)

    def test_upsert_updates_name_without_duplicate_airport(self):
        with sqlite3.connect(":memory:") as connection:
            ensure_table(connection)
            connection.execute(UPSERT_SQL, parse_airport_page(1, airport_page()))
            connection.execute(
                UPSERT_SQL, parse_airport_page(1, airport_page("Updated name"))
            )
            self.assertEqual(
                connection.execute(
                    "SELECT airport_id, airport_name, iata_code FROM airports"
                ).fetchall(),
                [(1, "Updated name", "ABS")],
            )

    def test_existing_schema_is_migrated_without_losing_data(self):
        with sqlite3.connect(":memory:") as connection:
            connection.execute(CREATE_TABLE_SQL.replace("    airport_name TEXT,\n", ""))
            connection.execute("ALTER TABLE airports ADD COLUMN latitude REAL")
            connection.execute(
                "INSERT INTO airports (airport_id, source_url, latitude) "
                "VALUES (1, 'old', 22.375)"
            )
            ensure_table(connection)
            ensure_table(connection)
            connection.execute(UPSERT_SQL, parse_airport_page(1, airport_page()))
            self.assertEqual(
                connection.execute(
                    "SELECT airport_name, latitude FROM airports"
                ).fetchone(),
                ("Abu Simbel", 22.375),
            )


if __name__ == "__main__":
    unittest.main()

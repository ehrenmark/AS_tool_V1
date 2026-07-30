import argparse
import os
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "airlinesim.sqlite3"
DEFAULT_ENTERPRISE_ID = 74
DEFAULT_BASE_URL = "https://hindenburg.airlinesim.aero"
DEFAULT_USER_AGENT = "AS-tool flightplan scraper (+local development)"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS flightplan_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id INTEGER NOT NULL,
    enterprise_name TEXT,
    flight_number TEXT NOT NULL,
    frequency TEXT,
    departure_time TEXT,
    arrival_time TEXT,
    aircraft_type TEXT,
    operator TEXT,
    remarks TEXT,
    validity TEXT,
    origin_airport_id INTEGER,
    origin_iata TEXT,
    origin_name TEXT,
    destination_airport_id INTEGER,
    destination_iata TEXT,
    destination_name TEXT,
    source_url TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(enterprise_id, flight_number, origin_airport_id, destination_airport_id, departure_time, arrival_time)
)
"""

UPSERT_SQL = """
INSERT INTO flightplan_flights (
    enterprise_id,
    enterprise_name,
    flight_number,
    frequency,
    departure_time,
    arrival_time,
    aircraft_type,
    operator,
    remarks,
    validity,
    origin_airport_id,
    origin_iata,
    origin_name,
    destination_airport_id,
    destination_iata,
    destination_name,
    source_url
) VALUES (
    :enterprise_id,
    :enterprise_name,
    :flight_number,
    :frequency,
    :departure_time,
    :arrival_time,
    :aircraft_type,
    :operator,
    :remarks,
    :validity,
    :origin_airport_id,
    :origin_iata,
    :origin_name,
    :destination_airport_id,
    :destination_iata,
    :destination_name,
    :source_url
)
ON CONFLICT(enterprise_id, flight_number, origin_airport_id, destination_airport_id, departure_time, arrival_time)
DO UPDATE SET
    enterprise_name = excluded.enterprise_name,
    frequency = excluded.frequency,
    aircraft_type = excluded.aircraft_type,
    operator = excluded.operator,
    remarks = excluded.remarks,
    validity = excluded.validity,
    origin_iata = excluded.origin_iata,
    origin_name = excluded.origin_name,
    destination_iata = excluded.destination_iata,
    destination_name = excluded.destination_name,
    source_url = excluded.source_url,
    scraped_at = CURRENT_TIMESTAMP
"""

AIRPORT_ID_PATTERN = re.compile(r"airports/(\d+)")


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def connect_to_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_TABLE_SQL)
    connection.commit()


def build_flightplan_url(base_url: str, enterprise_id: int) -> str:
    return urljoin(base_url, f"/app/info/enterprises/{enterprise_id}?&tab=3")


def extract_enterprise_name(soup: BeautifulSoup) -> str | None:
    title = soup.find("title")
    if not title:
        return None
    return normalize_text(title.get_text()).split(" | ")[0]


def parse_airport_header(row: Any) -> dict[str, Any]:
    link = row.find("a", href=AIRPORT_ID_PATTERN)
    if not link:
        raise ValueError("Airport-Link in Flugplan-Header nicht gefunden")

    href = link.get("href", "")
    id_match = AIRPORT_ID_PATTERN.search(href)
    if not id_match:
        raise ValueError(f"Airport-ID in Link nicht gefunden: {href}")

    spans = row.find_all("span")
    airport_name = normalize_text(spans[-1].get_text()) if spans else None

    return {
        "airport_id": int(id_match.group(1)),
        "iata": normalize_text(link.get_text()),
        "name": airport_name,
    }


def parse_flight_row(
    row: Any,
    enterprise_id: int,
    enterprise_name: str | None,
    origin: dict[str, Any],
    destination: dict[str, Any],
    source_url: str,
) -> dict[str, Any] | None:
    cells = row.find_all("td")
    if len(cells) < 8:
        return None

    flight_number = normalize_text(cells[0].get_text())
    if not flight_number:
        return None

    return {
        "enterprise_id": enterprise_id,
        "enterprise_name": enterprise_name,
        "flight_number": flight_number,
        "frequency": normalize_text(cells[1].get_text()),
        "departure_time": normalize_text(cells[2].get_text()),
        "arrival_time": normalize_text(cells[3].get_text()),
        "aircraft_type": normalize_text(cells[4].get_text()),
        "operator": normalize_text(cells[5].get_text()),
        "remarks": normalize_text(cells[6].get_text()),
        "validity": normalize_text(cells[7].get_text()),
        "origin_airport_id": origin["airport_id"],
        "origin_iata": origin["iata"],
        "origin_name": origin["name"],
        "destination_airport_id": destination["airport_id"],
        "destination_iata": destination["iata"],
        "destination_name": destination["name"],
        "source_url": source_url,
    }


def parse_flightplan(enterprise_id: int, source_url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        raise ValueError("Keine Flugplan-Tabelle gefunden")

    enterprise_name = extract_enterprise_name(soup)
    origin: dict[str, Any] | None = None
    destination: dict[str, Any] | None = None
    flights: list[dict[str, Any]] = []

    for row in table.find_all("tr"):
        classes = row.get("class") or []
        if "origin" in classes:
            origin = parse_airport_header(row)
            destination = None
        elif "destination" in classes:
            destination = parse_airport_header(row)
        elif "line" in classes and origin and destination:
            flight = parse_flight_row(row, enterprise_id, enterprise_name, origin, destination, source_url)
            if flight:
                flights.append(flight)

    return flights


def scrape_flightplan(args: argparse.Namespace) -> None:
    db_path = Path(args.db_path).expanduser().resolve()
    source_url = build_flightplan_url(args.base_url, args.enterprise_id)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})
    response = session.get(source_url, timeout=args.timeout)
    response.raise_for_status()

    flights = parse_flightplan(args.enterprise_id, source_url, response.text)
    if not flights:
        raise ValueError("Keine Flüge im Flugplan gefunden")

    connection = connect_to_sqlite(db_path)
    try:
        ensure_table(connection)
        if args.replace:
            connection.execute(
                "DELETE FROM flightplan_flights WHERE enterprise_id = ?",
                (args.enterprise_id,),
            )
        connection.executemany(UPSERT_SQL, flights)
        connection.commit()
    finally:
        connection.close()

    route_count = len({(f["origin_airport_id"], f["destination_airport_id"]) for f in flights})
    print(
        f"Fertig: {len(flights)} Flüge und {route_count} Routen gespeichert. "
        f"Datenbank: {db_path}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraped den AirlineSim-Flugplan einer Enterprise in SQLite."
    )
    parser.add_argument("--enterprise-id", type=int, default=DEFAULT_ENTERPRISE_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--db-path",
        default=os.getenv("AS_DB_PATH", str(DEFAULT_DB_PATH)),
        help="Pfad zur SQLite-Datenbankdatei",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Vor dem Import vorhandene Flüge dieser Enterprise löschen",
    )
    return parser.parse_args()


if __name__ == "__main__":
    scrape_flightplan(parse_args())
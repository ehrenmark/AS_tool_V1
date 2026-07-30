import argparse
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hindenburg.airlinesim.aero/app/info/airports/{airport_id}"
DEFAULT_USER_AGENT = "AS-tool airport scraper (+local development)"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "airlinesim.sqlite3"

FIELD_MAP = {
    "Aktuelle Ortszeit": "local_time",
    "Zeitzone": "timezone",
    "Aufkommensupdate": "traffic_update",
    "IATA Code": "iata_code",
    "ICAO Code": "icao_code",
    "IATA Area": "iata_area",
    "IATA Sub-Area": "iata_sub_area",
    "Land": "country",
    "Kontinent": "continent",
    "Landebahn": "runway",
    "Flughafengröße": "airport_size",
    "Slots (pro 5 Minuten)": "slots_per_5_minutes",
    "Slotverfügbarkeit": "slot_availability",
    "Mindesttransferzeit": "minimum_transfer_time",
    "Nachtflugverbot": "night_flight_ban",
    "Lärmschutzrestriktionen": "noise_restrictions",
    "Passagiere": "passenger_demand",
    "Fracht": "cargo_demand",
    "Current local time": "local_time",
    "Time zone": "timezone",
    "Demand calculation": "traffic_update",
    "IATA code": "iata_code",
    "ICAO code": "icao_code",
    "Country": "country",
    "Continent": "continent",
    "Runway": "runway",
    "Airport size": "airport_size",
    "Slots (per 5 minutes)": "slots_per_5_minutes",
    "Slot Availability": "slot_availability",
    "Min. transfer time": "minimum_transfer_time",
    "Nighttime ban": "night_flight_ban",
    "Noise restrictions": "noise_restrictions",
    "Passengers": "passenger_demand",
    "Cargo": "cargo_demand",
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS airports (
    airport_id INTEGER PRIMARY KEY,
    local_time TEXT,
    timezone TEXT,
    traffic_update TEXT,
    iata_code TEXT,
    icao_code TEXT,
    iata_area TEXT,
    iata_sub_area TEXT,
    country TEXT,
    continent TEXT,
    runway TEXT,
    airport_size TEXT,
    slots_per_5_minutes INTEGER,
    slot_availability TEXT,
    minimum_transfer_time TEXT,
    night_flight_ban TEXT,
    noise_restrictions TEXT,
    passenger_demand INTEGER,
    cargo_demand INTEGER,
    source_url TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

UPSERT_SQL = """
INSERT INTO airports (
    airport_id, local_time, timezone, traffic_update, iata_code, icao_code,
    iata_area, iata_sub_area, country, continent, runway, airport_size,
    slots_per_5_minutes, slot_availability, minimum_transfer_time,
    night_flight_ban, noise_restrictions, passenger_demand, cargo_demand,
    source_url
) VALUES (
    :airport_id, :local_time, :timezone, :traffic_update, :iata_code,
    :icao_code, :iata_area, :iata_sub_area, :country, :continent, :runway,
    :airport_size, :slots_per_5_minutes, :slot_availability,
    :minimum_transfer_time, :night_flight_ban, :noise_restrictions,
    :passenger_demand, :cargo_demand, :source_url
)
ON CONFLICT(airport_id) DO UPDATE SET
    local_time = excluded.local_time,
    timezone = excluded.timezone,
    traffic_update = excluded.traffic_update,
    iata_code = excluded.iata_code,
    icao_code = excluded.icao_code,
    iata_area = excluded.iata_area,
    iata_sub_area = excluded.iata_sub_area,
    country = excluded.country,
    continent = excluded.continent,
    runway = excluded.runway,
    airport_size = excluded.airport_size,
    slots_per_5_minutes = excluded.slots_per_5_minutes,
    slot_availability = excluded.slot_availability,
    minimum_transfer_time = excluded.minimum_transfer_time,
    night_flight_ban = excluded.night_flight_ban,
    noise_restrictions = excluded.noise_restrictions,
    passenger_demand = excluded.passenger_demand,
    cargo_demand = excluded.cargo_demand,
    source_url = excluded.source_url,
    scraped_at = CURRENT_TIMESTAMP
"""


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", value.replace(".", "").replace(",", ""))
    return int(match.group(0)) if match else None


def parse_demand(cell: Any) -> int | None:
    image = cell.find("img")
    if image and image.get("title"):
        return parse_int(image["title"])
    return parse_int(normalize_text(cell.get_text(" ", strip=True)))


def parse_airport_page(airport_id: int, html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    info_heading = soup.find(
        "h3",
        string=lambda value: bool(value)
        and normalize_text(value) in {"Informationen", "Information"},
    )
    if not info_heading:
        raise ValueError(f"Airport {airport_id}: Informationen-Abschnitt nicht gefunden")

    panel = info_heading.find_next("div", class_="as-panel")
    table = panel.find("table", class_="table") if panel else None
    if not table:
        raise ValueError(f"Airport {airport_id}: Informationen-Tabelle nicht gefunden")

    airport_data: dict[str, Any] = {column: None for column in FIELD_MAP.values()}
    airport_data["airport_id"] = airport_id
    airport_data["source_url"] = BASE_URL.format(airport_id=airport_id)

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        label = normalize_text(cells[0].get_text(" ", strip=True))
        column = FIELD_MAP.get(label)
        if not column:
            continue

        if column in {"passenger_demand", "cargo_demand"}:
            airport_data[column] = parse_demand(cells[1])
        elif column == "slots_per_5_minutes":
            airport_data[column] = parse_int(cells[1].get_text(" ", strip=True))
        else:
            airport_data[column] = normalize_text(cells[1].get_text(" ", strip=True))

    if not airport_data.get("iata_code"):
        raise ValueError(f"Airport {airport_id}: Keine Airport-Daten gefunden")

    return airport_data


def connect_to_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_TABLE_SQL)
    connection.commit()


def fetch_airport_html(session: requests.Session, airport_id: int, timeout: int) -> str | None:
    response = session.get(BASE_URL.format(airport_id=airport_id), timeout=timeout)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.text


def scrape_airports(args: argparse.Namespace) -> None:
    db_path = Path(args.db_path).expanduser().resolve()
    connection = connect_to_sqlite(db_path)
    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})

    saved_count = 0
    skipped_count = 0
    failed_count = 0
    empty_streak = 0

    print(f"SQLite-Datenbank: {db_path}")

    try:
        ensure_table(connection)

        for airport_id in range(args.start_id, args.end_id + 1):
            try:
                html = fetch_airport_html(session, airport_id, args.timeout)
                if html is None:
                    skipped_count += 1
                    empty_streak += 1
                    print(f"[{airport_id}] nicht gefunden, übersprungen")
                    if empty_streak >= args.stop_after_missing:
                        print(f"Abbruch nach {empty_streak} fehlenden IDs in Folge")
                        break
                    continue

                airport_data = parse_airport_page(airport_id, html)
                connection.execute(UPSERT_SQL, airport_data)
                connection.commit()
                saved_count += 1
                empty_streak = 0
                print(
                    f"[{airport_id}] gespeichert: "
                    f"{airport_data.get('iata_code')} {airport_data.get('country')}"
                )
            except (requests.RequestException, ValueError, sqlite3.Error) as error:
                connection.rollback()
                failed_count += 1
                empty_streak += 1
                print(f"[{airport_id}] Fehler: {error}")
                if empty_streak >= args.stop_after_missing:
                    print(f"Abbruch nach {empty_streak} fehlgeschlagenen/fehlenden IDs in Folge")
                    break

            if args.delay > 0:
                time.sleep(args.delay)
    finally:
        connection.close()

    print(
        "Fertig: "
        f"{saved_count} gespeichert, {skipped_count} übersprungen, {failed_count} fehlgeschlagen"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraped AirlineSim-Flughafeninformationen und speichert sie in SQLite."
    )
    parser.add_argument("--start-id", type=int, default=1, help="Erste Flughafen-ID")
    parser.add_argument("--end-id", type=int, default=10000, help="Letzte Flughafen-ID")
    parser.add_argument(
        "--stop-after-missing",
        type=int,
        default=100,
        help="Stoppt nach dieser Anzahl fehlender/fehlerhafter IDs in Folge",
    )
    parser.add_argument("--delay", type=float, default=0.5, help="Pause zwischen Requests in Sekunden")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP-Timeout in Sekunden")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="HTTP User-Agent")
    parser.add_argument(
        "--db-path",
        default=os.getenv("AS_DB_PATH", str(DEFAULT_DB_PATH)),
        help="Pfad zur SQLite-Datenbankdatei",
    )
    return parser.parse_args()


if __name__ == "__main__":
    scrape_airports(parse_args())
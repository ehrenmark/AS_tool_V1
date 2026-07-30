import argparse
import csv
import io
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "airlinesim.sqlite3"
DEFAULT_AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

COORDINATE_COLUMNS_SQL = {
    "latitude": "ALTER TABLE airports ADD COLUMN latitude REAL",
    "longitude": "ALTER TABLE airports ADD COLUMN longitude REAL",
    "coordinate_source": "ALTER TABLE airports ADD COLUMN coordinate_source TEXT",
    "coordinate_updated_at": "ALTER TABLE airports ADD COLUMN coordinate_updated_at TEXT",
}


def connect_to_sqlite(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite-Datenbank nicht gefunden: {db_path}")

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_coordinate_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(airports)").fetchall()
    }

    for column_name, alter_sql in COORDINATE_COLUMNS_SQL.items():
        if column_name not in existing_columns:
            connection.execute(alter_sql)

    connection.commit()


def load_ourairports_rows(source: str) -> list[dict[str, str]]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=60) as response:
            csv_text = response.read().decode("utf-8-sig")
    else:
        csv_text = Path(source).read_text(encoding="utf-8-sig")

    return list(csv.DictReader(io.StringIO(csv_text)))


def airport_row_has_coordinates(row: dict[str, str]) -> bool:
    return bool(row.get("latitude_deg") and row.get("longitude_deg"))


def index_coordinate_rows(rows: list[dict[str, str]]) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    by_icao: dict[str, dict[str, str]] = {}
    by_iata: dict[str, dict[str, str]] = {}

    for row in rows:
        if not airport_row_has_coordinates(row):
            continue

        ident = (row.get("ident") or "").strip().upper()
        iata = (row.get("iata_code") or "").strip().upper()

        if ident:
            by_icao[ident] = row
        if iata:
            by_iata[iata] = row

    return by_icao, by_iata


def find_coordinate_match(
    airport: sqlite3.Row,
    by_icao: dict[str, dict[str, str]],
    by_iata: dict[str, dict[str, str]],
) -> tuple[dict[str, str] | None, str | None]:
    icao_code = (airport["icao_code"] or "").strip().upper()
    iata_code = (airport["iata_code"] or "").strip().upper()

    if icao_code and icao_code in by_icao:
        return by_icao[icao_code], "OurAirports ICAO"
    if iata_code and iata_code in by_iata:
        return by_iata[iata_code], "OurAirports IATA"

    return None, None


def update_coordinates(args: argparse.Namespace) -> None:
    db_path = Path(args.db_path).expanduser().resolve()
    connection = connect_to_sqlite(db_path)

    try:
        ensure_coordinate_columns(connection)
        coordinate_rows = load_ourairports_rows(args.source)
        by_icao, by_iata = index_coordinate_rows(coordinate_rows)

        airports = connection.execute(
            """
            SELECT airport_id, iata_code, icao_code
            FROM airports
            WHERE latitude IS NULL OR longitude IS NULL OR :overwrite = 1
            ORDER BY airport_id
            """,
            {"overwrite": 1 if args.overwrite else 0},
        ).fetchall()

        updated_count = 0
        missing_count = 0

        for airport in airports:
            match, source_name = find_coordinate_match(airport, by_icao, by_iata)
            if not match or not source_name:
                missing_count += 1
                if args.verbose:
                    print(
                        f"[{airport['airport_id']}] keine Koordinaten gefunden "
                        f"({airport['iata_code']}/{airport['icao_code']})"
                    )
                continue

            latitude = float(match["latitude_deg"])
            longitude = float(match["longitude_deg"])
            connection.execute(
                """
                UPDATE airports
                SET latitude = :latitude,
                    longitude = :longitude,
                    coordinate_source = :coordinate_source,
                    coordinate_updated_at = CURRENT_TIMESTAMP
                WHERE airport_id = :airport_id
                """,
                {
                    "airport_id": airport["airport_id"],
                    "latitude": latitude,
                    "longitude": longitude,
                    "coordinate_source": source_name,
                },
            )
            updated_count += 1

            if args.verbose:
                print(
                    f"[{airport['airport_id']}] {airport['iata_code']}/{airport['icao_code']} -> "
                    f"{latitude}, {longitude}"
                )

        connection.commit()
    finally:
        connection.close()

    print(
        f"Fertig: {updated_count} Flughäfen aktualisiert, "
        f"{missing_count} ohne Treffer. Datenbank: {db_path}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ergänzt Koordinaten in der AirlineSim-SQLite-Datenbank."
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("AS_DB_PATH", str(DEFAULT_DB_PATH)),
        help="Pfad zur SQLite-Datenbankdatei",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_AIRPORTS_CSV_URL,
        help="OurAirports airports.csv URL oder lokaler CSV-Pfad",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Bestehende Koordinaten erneut überschreiben",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Gibt jeden Treffer und Fehlschlag aus",
    )
    return parser.parse_args()


if __name__ == "__main__":
    update_coordinates(parse_args())
import argparse
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "hindenburg_v5.sqlite3"
DEFAULT_BASE_URL = "https://hindenburg.airlinesim.aero"
DEFAULT_USER_AGENT = "AS-tool flightplan scraper (+local development)"

CREATE_AIRLINES_SQL = """
CREATE TABLE IF NOT EXISTS airlines (
    enterprise_id INTEGER PRIMARY KEY CHECK (enterprise_id > 0),
    enterprise_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    last_seen_at TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT
)
"""

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS flightplan_flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enterprise_id INTEGER NOT NULL REFERENCES airlines(enterprise_id),
    enterprise_name TEXT,
    flight_number TEXT NOT NULL CHECK (length(trim(flight_number)) > 0),
    frequency TEXT NOT NULL DEFAULT '',
    departure_time TEXT NOT NULL,
    arrival_time TEXT NOT NULL,
    aircraft_type TEXT,
    operator TEXT,
    remarks TEXT,
    validity TEXT NOT NULL DEFAULT '',
    is_via_summary INTEGER NOT NULL DEFAULT 0 CHECK (is_via_summary IN (0, 1)),
    origin_airport_id INTEGER NOT NULL CHECK (origin_airport_id > 0),
    origin_iata TEXT,
    origin_name TEXT,
    destination_airport_id INTEGER NOT NULL CHECK (destination_airport_id > 0),
    destination_iata TEXT,
    destination_name TEXT,
    source_url TEXT NOT NULL,
    scraped_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(enterprise_id, flight_number, origin_airport_id, destination_airport_id,
           departure_time, arrival_time, frequency, validity, is_via_summary)
)
"""

FLIGHT_COLUMNS = (
    "enterprise_id", "enterprise_name", "flight_number", "frequency",
    "departure_time", "arrival_time", "aircraft_type", "operator", "remarks",
    "validity", "origin_airport_id", "origin_iata", "origin_name",
    "destination_airport_id", "destination_iata", "destination_name", "source_url",
    "is_via_summary",
)
IDENTITY_COLUMNS = (
    "enterprise_id", "flight_number", "origin_airport_id", "destination_airport_id",
    "departure_time", "arrival_time", "frequency", "validity", "is_via_summary",
)
UPSERT_SQL = f"""
INSERT INTO flightplan_flights ({', '.join(FLIGHT_COLUMNS)})
VALUES ({', '.join(':' + column for column in FLIGHT_COLUMNS)})
ON CONFLICT({', '.join(IDENTITY_COLUMNS)}) DO UPDATE SET
    {', '.join(f'{column} = excluded.{column}' for column in FLIGHT_COLUMNS
               if column not in IDENTITY_COLUMNS)},
    scraped_at = CURRENT_TIMESTAMP
RETURNING id
"""

AIRPORT_ID_PATTERN = re.compile(r"airports/(\d+)")
ENTERPRISE_PATH_PATTERN = re.compile(r"^/app/info/enterprises/(\d+)/?$")


@dataclass
class ScrapeResult:
    enterprises_total: int
    enterprises_succeeded: int
    enterprises_failed: int
    flights_imported: int
    routes_imported: int
    failures: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FlightplanSnapshot:
    enterprise_name: str
    flights: list[dict[str, Any]]


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def connect_to_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.row_factory = sqlite3.Row
    return connection


def ensure_table(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(CREATE_AIRLINES_SQL)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(flightplan_flights)")}
        if columns and "is_via_summary" not in columns:
            cursor = connection.execute("SELECT * FROM flightplan_flights ORDER BY id")
            names = [column[0] for column in cursor.description]
            old_rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            connection.execute(CREATE_TABLE_SQL.replace("flightplan_flights", "flightplan_flights_new"))
            migration_sql = UPSERT_SQL.replace("flightplan_flights", "flightplan_flights_new")
            migration_sql = migration_sql.replace(
                f"({', '.join(FLIGHT_COLUMNS)})",
                f"(id, scraped_at, {', '.join(FLIGHT_COLUMNS)})",
                1,
            ).replace("VALUES (", "VALUES (:id, :scraped_at, ", 1)
            for row in old_rows:
                flight = prepare_flights(row["enterprise_id"], [row])[0]
                upsert_airline(
                    connection, row["enterprise_id"], row.get("enterprise_name"),
                    row["source_url"].split("/app/")[0],
                )
                connection.execute(migration_sql, {**flight, "id": row["id"],
                                                   "scraped_at": row["scraped_at"]}).fetchone()
            connection.execute("DROP TABLE flightplan_flights")
            connection.execute("ALTER TABLE flightplan_flights_new RENAME TO flightplan_flights")
        else:
            connection.execute(CREATE_TABLE_SQL)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_flightplan_enterprise ON flightplan_flights(enterprise_id)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def create_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=4, status_forcelist=(429, 502, 503, 504),
        backoff_factor=1, respect_retry_after_header=True,
    )))
    return session


def upsert_airline(
    connection: sqlite3.Connection,
    enterprise_id: int,
    enterprise_name: str | None,
    base_url: str,
) -> None:
    if type(enterprise_id) is not int or enterprise_id <= 0:
        raise ValueError("Airline-ID muss eine positive ganze Zahl sein")
    connection.execute(
        """
        INSERT INTO airlines (enterprise_id, enterprise_name, source_url)
        VALUES (?, COALESCE(?, 'Airline ' || ?), ?)
        ON CONFLICT(enterprise_id) DO UPDATE SET
            enterprise_name = COALESCE(?, airlines.enterprise_name),
            source_url = excluded.source_url
        """,
        (enterprise_id, enterprise_name, enterprise_id,
         urljoin(base_url, f"/app/info/enterprises/{enterprise_id}"), enterprise_name),
    )


def prepare_flights(enterprise_id: int, flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = {}
    for row in flights:
        flight = {column: row.get(column) for column in FLIGHT_COLUMNS}
        if flight["enterprise_id"] != enterprise_id:
            raise ValueError("Flugplan enthält Einträge einer anderen Airline")
        for column in ("origin_airport_id", "destination_airport_id"):
            if type(flight[column]) is not int or flight[column] <= 0:
                raise ValueError(f"Ungültiger Flughafen: {column}")
        for column in ("flight_number", "departure_time", "arrival_time", "source_url"):
            if not isinstance(flight[column], str) or not flight[column].strip():
                raise ValueError(f"Fehlender Flugplan-Wert: {column}")
            flight[column] = normalize_text(flight[column])
        for column in ("frequency", "validity"):
            flight[column] = normalize_text(flight[column] or "")
        flight["is_via_summary"] = int(bool(re.search(
            r"\bvia\b", flight["remarks"] or "", re.IGNORECASE,
        )))
        key = tuple(flight[column] for column in IDENTITY_COLUMNS)
        if key in prepared and prepared[key] != flight:
            raise ValueError(f"Widersprüchliche Flugplan-Zeilen: {flight['flight_number']}")
        prepared[key] = flight
    return list(prepared.values())


def build_flightplan_url(base_url: str, enterprise_id: int) -> str:
    return urljoin(base_url, f"/app/info/enterprises/{enterprise_id}?tab=3")


def build_enterprise_directory_url(base_url: str, letter: str | None = None) -> str:
    url = urljoin(base_url, "/app/info/enterprises")
    return f"{url}?{urlencode({'letter': letter})}" if letter else url


def parse_directory_letters(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    letters = set()
    for anchor in soup.select("a[href]"):
        url = urlparse(urljoin(base_url, anchor.get("href", "")))
        query = parse_qs(url.query)
        letter = query.get("letter", [""])[0].strip().upper()
        if url.path == "/app/info/enterprises" and len(letter) == 1 and letter.isalpha():
            letters.add(letter)
    return sorted(letters)


def parse_enterprise_directory(base_url: str, html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.enterprises")
    empty_message = "No entries exist for the selected letter" in soup.get_text(" ", strip=True)
    if table is None:
        if empty_message:
            return []
        raise ValueError("Enterprise-Tabelle im Verzeichnis nicht gefunden")

    enterprises = {}
    for row in table.select("tbody tr"):
        entries = []
        for anchor in row.select("a[href]"):
            path = urlparse(urljoin(base_url, anchor.get("href", ""))).path
            match = ENTERPRISE_PATH_PATTERN.fullmatch(path)
            name = normalize_text(anchor.get_text())
            if match and name and int(match.group(1)) > 0:
                entries.append((int(match.group(1)), name))
        if len(entries) != 1:
            raise ValueError("Keine gültigen Enterprises in einer Verzeichnis-Zeile gefunden")
        enterprise_id, name = entries[0]
        if enterprise_id in enterprises and enterprises[enterprise_id] != name:
            raise ValueError(f"Widersprüchlicher Airline-Name für {enterprise_id}")
        enterprises[enterprise_id] = name
    result = [
        {"enterprise_id": enterprise_id, "enterprise_name": name}
        for enterprise_id, name in sorted(enterprises.items(), key=lambda item: item[1].casefold())
    ]
    if not result and not empty_message:
        raise ValueError("Keine gültigen Enterprises in der Verzeichnis-Tabelle gefunden")
    return result


def discover_enterprises(session: requests.Session, base_url: str, timeout: int) -> list[dict[str, Any]]:
    directory_url = build_enterprise_directory_url(base_url)
    response = session.get(directory_url, timeout=timeout)
    response.raise_for_status()
    letters = parse_directory_letters(directory_url, response.text)
    if not letters:
        raise ValueError("Keine Buchstaben im Enterprise-Verzeichnis gefunden")

    enterprises = {}
    for letter in letters:
        response = session.get(build_enterprise_directory_url(base_url, letter), timeout=timeout)
        response.raise_for_status()
        for enterprise in parse_enterprise_directory(response.url, response.text):
            enterprises[enterprise["enterprise_id"]] = enterprise
    if not enterprises:
        raise ValueError("Keine Enterprises im Verzeichnis gefunden")
    return sorted(enterprises.values(), key=lambda item: item["enterprise_name"].casefold())


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
) -> dict[str, Any]:
    cells = row.find_all("td")
    if len(cells) < 8:
        raise ValueError("Unvollständige Flugplan-Zeile")

    flight_number = normalize_text(cells[0].get_text(" ", strip=True))
    if not flight_number:
        raise ValueError("Flugnummer in Flugplan-Zeile fehlt")

    return {
        "enterprise_id": enterprise_id,
        "enterprise_name": enterprise_name,
        "flight_number": flight_number,
        "frequency": normalize_text(cells[1].get_text(" ", strip=True)),
        "departure_time": normalize_text(cells[2].get_text(" ", strip=True)),
        "arrival_time": normalize_text(cells[3].get_text(" ", strip=True)),
        "aircraft_type": normalize_text(cells[4].get_text(" ", strip=True)),
        "operator": normalize_text(cells[5].get_text(" ", strip=True)),
        "remarks": normalize_text(cells[6].get_text(" ", strip=True)),
        "validity": normalize_text(cells[7].get_text(" ", strip=True)),
        "origin_airport_id": origin["airport_id"],
        "origin_iata": origin["iata"],
        "origin_name": origin["name"],
        "destination_airport_id": destination["airport_id"],
        "destination_iata": destination["iata"],
        "destination_name": destination["name"],
        "source_url": source_url,
    }


def parse_flightplan(
    enterprise_id: int,
    source_url: str,
    html: str,
    enterprise_name: str | None = None,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    candidates = [item for item in tables if item.select_one("tr.origin, tr.line")]
    if len(candidates) > 1:
        raise ValueError("Mehrere Flugplan-Tabellen gefunden")
    table = candidates[0] if candidates else None
    if not table:
        page_text = soup.get_text(" ", strip=True)
        title_name = extract_enterprise_name(soup)
        empty_message = any(message in page_text for message in (
            "No data available.", "Keine Daten vorhanden.", "Keine Daten verfügbar.",
        ))
        if (not tables and title_name and empty_message
                and ("Flight schedule" in page_text or "Flugplan" in page_text)
                and any(normalize_text(heading.get_text()) == title_name
                        for heading in soup.find_all("h2"))):
            return []
        if tables:
            raise ValueError("Keine gültigen Flugplan-Zeilen in der Tabelle gefunden")
        raise ValueError("Unerwartete Flugplan-Seite ohne Tabelle")

    enterprise_name = extract_enterprise_name(soup) or enterprise_name
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
        elif "line" in classes:
            if not origin or not destination:
                raise ValueError("Flugplan-Zeile ohne Start- oder Zielflughafen")
            flight = parse_flight_row(row, enterprise_id, enterprise_name, origin, destination, source_url)
            flights.append(flight)

    if not flights:
        raise ValueError("Keine gültigen Flugplan-Zeilen in der Tabelle gefunden")
    return prepare_flights(enterprise_id, flights)


def fetch_flightplan(
    session: requests.Session,
    base_url: str,
    enterprise_id: int,
    enterprise_name: str | None,
    timeout: int,
) -> FlightplanSnapshot:
    source_url = build_flightplan_url(base_url, enterprise_id)
    response = session.get(source_url, timeout=timeout)
    response.raise_for_status()
    if urlparse(response.url).path.rstrip("/") != f"/app/info/enterprises/{enterprise_id}":
        raise ValueError("Flugplan-Abruf wurde auf eine unerwartete Seite umgeleitet")
    flights = parse_flightplan(enterprise_id, response.url, response.text, enterprise_name)
    name = extract_enterprise_name(BeautifulSoup(response.text, "html.parser")) or enterprise_name
    if not name:
        raise ValueError("Airline-Name auf der Flugplan-Seite fehlt")
    return FlightplanSnapshot(name, flights)


def store_directory(
    connection: sqlite3.Connection,
    enterprises: list[dict[str, Any]],
    base_url: str,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    if not enterprises:
        raise ValueError("Leeres Airline-Verzeichnis wird nicht übernommen")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if transaction_guard:
            transaction_guard(connection)
        connection.execute("UPDATE airlines SET is_active = 0")
        for enterprise in enterprises:
            upsert_airline(connection, enterprise["enterprise_id"],
                           enterprise["enterprise_name"], base_url)
            connection.execute(
                "UPDATE airlines SET is_active = 1, last_seen_at = CURRENT_TIMESTAMP "
                "WHERE enterprise_id = ?", (enterprise["enterprise_id"],),
            )
        connection.execute(
            "DELETE FROM flightplan_flights WHERE enterprise_id IN "
            "(SELECT enterprise_id FROM airlines WHERE is_active = 0)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def store_flightplan(
    connection: sqlite3.Connection,
    enterprise_id: int,
    flights: list[dict[str, Any]],
    replace: bool = True,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
    *,
    enterprise_name: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> int:
    try:
        connection.execute("BEGIN IMMEDIATE")
        if transaction_guard:
            transaction_guard(connection)
        prepared = prepare_flights(enterprise_id, flights)
        enterprise_name = enterprise_name or next(
            (flight["enterprise_name"] for flight in prepared if flight["enterprise_name"]), None,
        )
        upsert_airline(connection, enterprise_id, enterprise_name, base_url)
        connection.execute("CREATE TEMP TABLE IF NOT EXISTS snapshot_ids (id INTEGER PRIMARY KEY)")
        connection.execute("DELETE FROM snapshot_ids")
        for flight in prepared:
            flight["enterprise_name"] = enterprise_name
            flight_id = connection.execute(UPSERT_SQL, flight).fetchone()[0]
            connection.execute("INSERT INTO snapshot_ids VALUES (?)", (flight_id,))
        if replace:
            connection.execute(
                "DELETE FROM flightplan_flights WHERE enterprise_id = ? "
                "AND id NOT IN (SELECT id FROM snapshot_ids)", (enterprise_id,),
            )
        connection.execute(
            "UPDATE airlines SET is_active = 1, last_attempt_at = CURRENT_TIMESTAMP, "
            "last_success_at = CURRENT_TIMESTAMP, last_error = NULL WHERE enterprise_id = ?",
            (enterprise_id,),
        )
        connection.commit()
        return len(prepared)
    except Exception:
        connection.rollback()
        raise


def scrape_enterprises(
    db_path: Path,
    enterprises: list[dict[str, Any]],
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 60,
    request_delay: float = 0.75,
    user_agent: str = DEFAULT_USER_AGENT,
    replace: bool = True,
    reject_empty: bool = False,
    session: requests.Session | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> ScrapeResult:
    owns_session = session is None
    session = session or create_session(user_agent)
    session.headers.update({"User-Agent": user_agent})
    connection = connect_to_sqlite(db_path)
    total_flights = 0
    total_routes = 0
    successful_enterprises = 0
    failures = []
    try:
        ensure_table(connection)
        for index, enterprise in enumerate(enterprises):
            enterprise_id = enterprise["enterprise_id"]
            enterprise_name = enterprise["enterprise_name"]
            try:
                snapshot = fetch_flightplan(
                    session,
                    base_url,
                    enterprise_id,
                    enterprise_name,
                    timeout,
                )
                flights = snapshot.flights
                if reject_empty and not flights:
                    raise ValueError("Keine Flüge im Flugplan gefunden")
                if progress_callback:
                    progress_callback()
                transaction_guard = getattr(progress_callback, "assert_transaction", None)
                stored_count = store_flightplan(
                    connection, enterprise_id, flights, replace,
                    transaction_guard=transaction_guard,
                    enterprise_name=snapshot.enterprise_name,
                    base_url=base_url,
                )
                successful_enterprises += 1
                total_flights += stored_count
                total_routes += len({
                    (flight["origin_airport_id"], flight["destination_airport_id"])
                    for flight in flights
                })
            except (requests.RequestException, ValueError, sqlite3.Error) as error:
                connection.rollback()
                failures.append(f"{enterprise_name or enterprise_id}: {error}")
                connection.execute("BEGIN IMMEDIATE")
                transaction_guard = getattr(progress_callback, "assert_transaction", None)
                if transaction_guard:
                    transaction_guard(connection)
                upsert_airline(connection, enterprise_id, enterprise_name, base_url)
                connection.execute(
                    "UPDATE airlines SET last_attempt_at = CURRENT_TIMESTAMP, last_error = ? "
                    "WHERE enterprise_id = ?", (str(error), enterprise_id),
                )
                connection.commit()

            if progress_callback:
                progress_callback()
            if index < len(enterprises) - 1:
                time.sleep(request_delay)
    finally:
        connection.close()
        if owns_session:
            session.close()

    return ScrapeResult(
        enterprises_total=len(enterprises),
        enterprises_succeeded=successful_enterprises,
        enterprises_failed=len(failures),
        flights_imported=total_flights,
        routes_imported=total_routes,
        failures=failures,
    )


def scrape_all_enterprises(
    db_path: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 60,
    request_delay: float = 0.75,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> ScrapeResult:
    owns_session = session is None
    session = session or create_session(user_agent)
    session.headers.update({"User-Agent": user_agent})
    try:
        enterprises = discover_enterprises(session, base_url, timeout)
        if progress_callback:
            progress_callback()
        connection = connect_to_sqlite(db_path)
        try:
            ensure_table(connection)
            store_directory(connection, enterprises, base_url,
                            getattr(progress_callback, "assert_transaction", None))
        finally:
            connection.close()
        return scrape_enterprises(
            db_path,
            enterprises,
            base_url=base_url,
            timeout=timeout,
            request_delay=request_delay,
            user_agent=user_agent,
            replace=True,
            session=session,
            progress_callback=progress_callback,
        )
    finally:
        if owns_session:
            session.close()


def scrape_flightplan(args: argparse.Namespace) -> None:
    try:
        from jobs.refresh_flightplans import run_refresh
    except ModuleNotFoundError:
        from refresh_flightplans import run_refresh

    db_path = Path(args.db_path).expanduser().resolve()
    options = {
        "base_url": args.base_url, "timeout": args.timeout,
        "request_delay": args.request_delay, "user_agent": args.user_agent,
    }
    if args.all_enterprises:
        scraper = scrape_all_enterprises
    else:
        scraper = scrape_enterprises
        options.update({
            "enterprises": [{"enterprise_id": args.enterprise_id, "enterprise_name": None}],
            "replace": args.replace,
        })
    result = run_refresh(db_path, stale_after_seconds=7200, scraper=scraper, **options)
    if result is None:
        raise RuntimeError("Ein anderer Flugplan-Import läuft bereits")

    print(
        f"Fertig: {result.flights_imported} Flüge und {result.routes_imported} Routen von "
        f"{result.enterprises_succeeded}/{result.enterprises_total} Enterprises verarbeitet. "
        f"Datenbank: {db_path}"
    )
    if result.failures:
        raise RuntimeError("Fehler bei einzelnen Enterprises:\n- " + "\n- ".join(result.failures))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraped den AirlineSim-Flugplan einer Enterprise in SQLite."
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--enterprise-id", type=int)
    selection.add_argument(
        "--all-enterprises",
        action="store_true",
        help="Alle Enterprises aus dem öffentlichen Server-Verzeichnis verarbeiten",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--db-path",
        default=os.getenv("AS_DB_PATH", str(DEFAULT_DB_PATH)),
        help="Pfad zur SQLite-Datenbankdatei",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.75,
        help="Pause zwischen Flugplan-Anfragen im Alle-Enterprises-Modus",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--replace",
        action="store_true",
        default=True,
        help="Vollständigen Airline-Flugplan synchronisieren (Standard)",
    )
    mode.add_argument(
        "--append", action="store_false", dest="replace",
        help="Nur hinzufügen/aktualisieren, entfallene Flüge nicht entfernen",
    )
    args = parser.parse_args()
    if args.enterprise_id is not None and args.enterprise_id <= 0:
        parser.error("--enterprise-id muss positiv sein")
    if args.all_enterprises and not args.replace:
        parser.error("--append ist nur für einzelne Airlines verfügbar")
    if args.request_delay < 0:
        parser.error("--request-delay darf nicht negativ sein")
    return args


if __name__ == "__main__":
    scrape_flightplan(parse_args())
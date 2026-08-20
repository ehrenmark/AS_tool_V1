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
    for anchor in table.select("tbody a[href]"):
        path = urlparse(urljoin(base_url, anchor.get("href", ""))).path
        match = ENTERPRISE_PATH_PATTERN.fullmatch(path)
        name = normalize_text(anchor.get_text())
        if match and name:
            enterprise_id = int(match.group(1))
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


def parse_flightplan(
    enterprise_id: int,
    source_url: str,
    html: str,
    enterprise_name: str | None = None,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        page_text = soup.get_text(" ", strip=True)
        title_name = extract_enterprise_name(soup)
        if title_name and "Flight schedule" in page_text:
            return []
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
        elif "line" in classes and origin and destination:
            flight = parse_flight_row(row, enterprise_id, enterprise_name, origin, destination, source_url)
            if flight:
                flights.append(flight)

    if not flights:
        raise ValueError("Keine gültigen Flugplan-Zeilen in der Tabelle gefunden")
    return flights


def fetch_flightplan(
    session: requests.Session,
    base_url: str,
    enterprise_id: int,
    enterprise_name: str | None,
    timeout: int,
) -> list[dict[str, Any]]:
    source_url = build_flightplan_url(base_url, enterprise_id)
    response = session.get(source_url, timeout=timeout)
    response.raise_for_status()
    return parse_flightplan(enterprise_id, response.url, response.text, enterprise_name)


def store_flightplan(
    connection: sqlite3.Connection,
    enterprise_id: int,
    flights: list[dict[str, Any]],
    replace: bool,
    transaction_guard: Callable[[sqlite3.Connection], None] | None = None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        if transaction_guard:
            transaction_guard(connection)
        if replace:
            connection.execute(
                "DELETE FROM flightplan_flights WHERE enterprise_id = ?",
                (enterprise_id,),
            )
        if flights:
            connection.executemany(UPSERT_SQL, flights)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def scrape_enterprises(
    db_path: Path,
    enterprises: list[dict[str, Any]],
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 60,
    request_delay: float = 0.25,
    user_agent: str = DEFAULT_USER_AGENT,
    replace: bool = True,
    reject_empty: bool = False,
    session: requests.Session | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> ScrapeResult:
    session = session or requests.Session()
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
                flights = fetch_flightplan(
                    session,
                    base_url,
                    enterprise_id,
                    enterprise_name,
                    timeout,
                )
                if reject_empty and not flights:
                    raise ValueError("Keine Flüge im Flugplan gefunden")
                if progress_callback:
                    progress_callback()
                transaction_guard = getattr(progress_callback, "assert_transaction", None)
                store_flightplan(
                    connection, enterprise_id, flights, replace,
                    transaction_guard=transaction_guard,
                )
                successful_enterprises += 1
                total_flights += len(flights)
                total_routes += len({
                    (flight["origin_airport_id"], flight["destination_airport_id"])
                    for flight in flights
                })
            except (requests.RequestException, ValueError, sqlite3.Error) as error:
                connection.rollback()
                failures.append(f"{enterprise_name or enterprise_id}: {error}")

            if progress_callback:
                progress_callback()
            if index < len(enterprises) - 1:
                time.sleep(request_delay)
    finally:
        connection.close()

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
    request_delay: float = 0.25,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
    progress_callback: Callable[[], None] | None = None,
) -> ScrapeResult:
    session = session or requests.Session()
    session.headers.update({"User-Agent": user_agent})
    enterprises = discover_enterprises(session, base_url, timeout)
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


def scrape_flightplan(args: argparse.Namespace) -> None:
    db_path = Path(args.db_path).expanduser().resolve()
    if args.all_enterprises:
        result = scrape_all_enterprises(
            db_path,
            base_url=args.base_url,
            timeout=args.timeout,
            request_delay=args.request_delay,
            user_agent=args.user_agent,
        )
    else:
        result = scrape_enterprises(
            db_path,
            [{"enterprise_id": args.enterprise_id, "enterprise_name": None}],
            base_url=args.base_url,
            timeout=args.timeout,
            request_delay=0,
            user_agent=args.user_agent,
            replace=args.replace,
            reject_empty=True,
        )

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
    parser.add_argument("--enterprise-id", type=int, default=DEFAULT_ENTERPRISE_ID)
    parser.add_argument(
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
        default=0.25,
        help="Pause zwischen Flugplan-Anfragen im Alle-Enterprises-Modus",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Vor dem Import vorhandene Flüge dieser Enterprise löschen",
    )
    return parser.parse_args()


if __name__ == "__main__":
    scrape_flightplan(parse_args())
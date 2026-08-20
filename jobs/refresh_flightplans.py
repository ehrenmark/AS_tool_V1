import argparse
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

try:
    from jobs.scrape_flightplan import (
        DEFAULT_BASE_URL,
        DEFAULT_DB_PATH,
        DEFAULT_USER_AGENT,
        ScrapeResult,
        scrape_all_enterprises,
    )
except ModuleNotFoundError:
    from scrape_flightplan import (
        DEFAULT_BASE_URL,
        DEFAULT_DB_PATH,
        DEFAULT_USER_AGENT,
        ScrapeResult,
        scrape_all_enterprises,
    )

CREATE_IMPORT_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_type TEXT NOT NULL DEFAULT 'flightplans',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    heartbeat_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failure')),
    enterprises_total INTEGER NOT NULL DEFAULT 0,
    enterprises_succeeded INTEGER NOT NULL DEFAULT 0,
    enterprises_failed INTEGER NOT NULL DEFAULT 0,
    flights_imported INTEGER NOT NULL DEFAULT 0,
    routes_imported INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT
)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_database(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_import_runs_table(connection: sqlite3.Connection) -> None:
    connection.execute(CREATE_IMPORT_RUNS_SQL)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_runs_active "
        "ON import_runs(import_type, status, heartbeat_at)"
    )
    connection.commit()


def acquire_run(db_path: Path, stale_after_seconds: int) -> int | None:
    connection = connect_database(db_path)
    try:
        ensure_import_runs_table(connection)
        now = utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        ).isoformat(timespec="seconds")
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """
            SELECT id FROM import_runs
            WHERE import_type = 'flightplans' AND status = 'running'
              AND heartbeat_at >= ?
            LIMIT 1
            """,
            (stale_before,),
        ).fetchone()
        if active:
            connection.rollback()
            return None
        connection.execute(
            """
            UPDATE import_runs
            SET status = 'failure', completed_at = ?,
                error_summary = 'Recovered stale running refresh'
            WHERE import_type = 'flightplans' AND status = 'running'
            """,
            (now,),
        )
        cursor = connection.execute(
            """
            INSERT INTO import_runs (import_type, started_at, heartbeat_at, status)
            VALUES ('flightplans', ?, ?, 'running')
            """,
            (now, now),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def heartbeat(db_path: Path, run_id: int) -> None:
    connection = connect_database(db_path)
    try:
        cursor = connection.execute(
            "UPDATE import_runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
            (utc_now(), run_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError("Refresh lease lost")
        connection.commit()
    finally:
        connection.close()


class LeaseHeartbeat:
    def __init__(self, db_path: Path, run_id: int):
        self.db_path = db_path
        self.run_id = run_id

    def __call__(self) -> None:
        heartbeat(self.db_path, self.run_id)

    def assert_transaction(self, connection: sqlite3.Connection) -> None:
        active = connection.execute(
            "SELECT 1 FROM import_runs WHERE id = ? AND status = 'running'",
            (self.run_id,),
        ).fetchone()
        if active is None:
            raise RuntimeError("Refresh lease lost")


def complete_run(db_path: Path, run_id: int, result: ScrapeResult) -> None:
    if result.enterprises_failed == 0:
        status = "success"
    elif result.enterprises_succeeded:
        status = "partial"
    else:
        status = "failure"
    connection = connect_database(db_path)
    try:
        connection.execute(
            """
            UPDATE import_runs SET completed_at = ?, heartbeat_at = ?, status = ?,
                enterprises_total = ?, enterprises_succeeded = ?, enterprises_failed = ?,
                flights_imported = ?, routes_imported = ?, error_summary = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                utc_now(), utc_now(), status, result.enterprises_total,
                result.enterprises_succeeded, result.enterprises_failed,
                result.flights_imported, result.routes_imported,
                "\n".join(result.failures) or None, run_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def fail_run(db_path: Path, run_id: int, error: Exception) -> None:
    connection = connect_database(db_path)
    try:
        connection.execute(
            """
            UPDATE import_runs
            SET completed_at = ?, heartbeat_at = ?, status = 'failure', error_summary = ?
            WHERE id = ? AND status = 'running'
            """,
            (utc_now(), utc_now(), str(error)[:4000], run_id),
        )
        connection.commit()
    finally:
        connection.close()


def run_refresh(
    db_path: Path,
    *,
    stale_after_seconds: int,
    scraper: Callable[..., ScrapeResult] = scrape_all_enterprises,
    **scraper_options: object,
) -> ScrapeResult | None:
    run_id = acquire_run(db_path, stale_after_seconds)
    if run_id is None:
        return None
    try:
        result = scraper(
            db_path,
            progress_callback=LeaseHeartbeat(db_path, run_id),
            **scraper_options,
        )
        complete_run(db_path, run_id, result)
        return result
    except Exception as error:
        fail_run(db_path, run_id, error)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh all flightplans safely in SQLite.")
    parser.add_argument("--db-path", default=os.getenv("AS_DB_PATH", str(DEFAULT_DB_PATH)))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("AS_FLIGHTPLAN_REFRESH_INTERVAL", "0")),
        help="Seconds between refresh starts; zero runs once",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=int(os.getenv("AS_FLIGHTPLAN_REFRESH_STALE_AFTER", "7200")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path).expanduser().resolve()
    while True:
        started = time.monotonic()
        try:
            result = run_refresh(
                db_path,
                stale_after_seconds=args.stale_after,
                base_url=args.base_url,
                timeout=args.timeout,
                request_delay=args.request_delay,
                user_agent=args.user_agent,
            )
            if result is None:
                print("Refresh skipped: another flightplan refresh is running.")
            else:
                print(
                    f"Refresh complete: {result.enterprises_succeeded}/"
                    f"{result.enterprises_total} enterprises, {result.flights_imported} flights."
                )
        except Exception as error:
            print(f"Refresh failed: {error}")
            if args.interval <= 0:
                raise
        if args.interval <= 0:
            return
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()

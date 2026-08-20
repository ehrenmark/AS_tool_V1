import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobs.refresh_flightplans import (
    ScrapeResult,
    acquire_run,
    ensure_import_runs_table,
    heartbeat,
    run_refresh,
)


class RefreshWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "refresh.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def rows(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute("SELECT * FROM import_runs ORDER BY id").fetchall()
        finally:
            connection.close()

    def test_successful_refresh_is_persisted_with_totals(self):
        expected = ScrapeResult(2, 2, 0, 17, 5, [])

        def scraper(db_path, progress_callback):
            self.assertEqual(db_path, self.db_path)
            progress_callback()
            return expected

        result = run_refresh(self.db_path, stale_after_seconds=60, scraper=scraper)

        self.assertEqual(result, expected)
        row = self.rows()[0]
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["enterprises_total"], 2)
        self.assertEqual(row["flights_imported"], 17)
        self.assertIsNotNone(row["completed_at"])

    def test_partial_refresh_records_failures(self):
        result = ScrapeResult(3, 2, 1, 8, 4, ["Broken Air: invalid page"])

        run_refresh(
            self.db_path,
            stale_after_seconds=60,
            scraper=lambda db_path, **kwargs: result,
        )

        row = self.rows()[0]
        self.assertEqual(row["status"], "partial")
        self.assertEqual(row["enterprises_failed"], 1)
        self.assertIn("Broken Air", row["error_summary"])

    def test_active_run_prevents_overlap(self):
        first_run = acquire_run(self.db_path, stale_after_seconds=60)

        self.assertIsNotNone(first_run)
        self.assertIsNone(acquire_run(self.db_path, stale_after_seconds=60))
        self.assertEqual(len(self.rows()), 1)

    def test_stale_run_is_recovered_before_new_run(self):
        connection = sqlite3.connect(self.db_path)
        ensure_import_runs_table(connection)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO import_runs (started_at, heartbeat_at, status)
            VALUES (?, ?, 'running')
            """,
            (old, old),
        )
        connection.commit()
        connection.close()

        new_run = acquire_run(self.db_path, stale_after_seconds=60)

        rows = self.rows()
        self.assertEqual(rows[0]["status"], "failure")
        self.assertIn("stale", rows[0]["error_summary"])
        self.assertEqual(rows[1]["id"], new_run)
        self.assertEqual(rows[1]["status"], "running")

    def test_unhandled_failure_completes_run(self):
        def scraper(db_path, **kwargs):
            raise RuntimeError("directory unavailable")

        with self.assertRaisesRegex(RuntimeError, "directory unavailable"):
            run_refresh(self.db_path, stale_after_seconds=60, scraper=scraper)

        row = self.rows()[0]
        self.assertEqual(row["status"], "failure")
        self.assertEqual(row["error_summary"], "directory unavailable")
        self.assertIsNotNone(row["completed_at"])

    def test_recovered_stale_run_cannot_renew_its_lease(self):
        stale_run = acquire_run(self.db_path, stale_after_seconds=60)
        connection = sqlite3.connect(self.db_path)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        connection.execute("UPDATE import_runs SET heartbeat_at = ? WHERE id = ?", (old, stale_run))
        connection.commit()
        connection.close()

        self.assertIsNotNone(acquire_run(self.db_path, stale_after_seconds=60))
        with self.assertRaisesRegex(RuntimeError, "lease lost"):
            heartbeat(self.db_path, stale_run)


if __name__ == "__main__":
    unittest.main()

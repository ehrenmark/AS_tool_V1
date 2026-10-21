import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Thread as RealThread
from unittest.mock import patch

import app as web
from jobs.refresh_flightplans import acquire_run
from jobs.scrape_flightplan import (
    FlightplanSnapshot,
    connect_to_sqlite,
    ensure_table,
    store_flightplan,
)


def flight(enterprise_id, number):
    return {
        "enterprise_id": enterprise_id, "enterprise_name": f"Airline {enterprise_id}",
        "flight_number": number, "frequency": "1234567",
        "departure_time": "10:00", "arrival_time": "12:00",
        "origin_airport_id": 1, "destination_airport_id": 2,
        "source_url": f"https://hindenburg.airlinesim.aero/app/info/enterprises/{enterprise_id}",
    }


class AirlineRefreshApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "test.sqlite3"
        with closing(connect_to_sqlite(self.db_path)) as connection:
            ensure_table(connection)
            store_flightplan(connection, 80, [flight(80, "OLD")])
            store_flightplan(connection, 90, [flight(90, "OTHER")])
        self.path_patch = patch.object(web, "DATABASE_PATH", self.db_path)
        self.path_patch.start()
        self.thread_patch = patch.object(web, "Thread")
        self.thread = self.thread_patch.start()
        self.client = web.app.test_client()

    def tearDown(self):
        self.thread_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def start(self, enterprise_id=80):
        return self.client.post(
            f"/api/flightplan/airlines/{enterprise_id}/refresh",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    def execute_worker(self):
        options = self.thread.call_args.kwargs
        options["target"](*options["args"])

    def flights(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(
                "SELECT id, enterprise_id, flight_number FROM flightplan_flights ORDER BY id"
            ).fetchall()

    def test_start_returns_run_id_and_prevents_overlapping_imports(self):
        response = self.start()
        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertEqual(body["enterprise_id"], 80)
        self.thread.return_value.start.assert_called_once()
        self.assertTrue(self.thread.call_args.kwargs["daemon"])
        status = self.client.get(f"/api/flightplan/refresh/{body['run_id']}")
        self.assertEqual(status.get_json()["run"]["status"], "running")
        self.assertEqual(status.headers["Cache-Control"], "no-store")
        self.assertEqual(self.start(90).status_code, 409)
        self.assertEqual(self.thread.call_count, 1)

    def test_background_update_only_replaces_the_selected_airline(self):
        other = self.flights()[1]
        response = self.start()
        run_id = response.get_json()["run_id"]
        with patch("jobs.scrape_flightplan.fetch_flightplan", return_value=FlightplanSnapshot(
            "Updated airline", [flight(80, "NEW")],
        )):
            self.execute_worker()
        rows = self.flights()
        self.assertIn(other, rows)
        self.assertEqual({row[2] for row in rows}, {"NEW", "OTHER"})
        status = self.client.get(f"/api/flightplan/refresh/{run_id}").get_json()["run"]
        self.assertEqual(status["status"], "success")
        self.assertEqual(status["flights_imported"], 1)
        self.assertIsNotNone(status["completed_at"])

    def test_real_background_thread_completes_through_the_api(self):
        threads = []

        def thread_factory(**options):
            thread = RealThread(**options)
            threads.append(thread)
            return thread

        self.thread.side_effect = thread_factory
        with patch("jobs.scrape_flightplan.fetch_flightplan",
                   return_value=FlightplanSnapshot("Airline 80", [flight(80, "NEW")])):
            response = self.start()
            self.assertEqual(response.status_code, 202)
            threads[0].join(timeout=10)
            self.assertFalse(threads[0].is_alive())
        status = self.client.get(
            f"/api/flightplan/refresh/{response.get_json()['run_id']}"
        ).get_json()["run"]
        self.assertEqual(status["status"], "success")
        self.assertEqual({row[2] for row in self.flights()}, {"NEW", "OTHER"})

    def test_failed_fetch_preserves_flights_and_reports_failure(self):
        before = self.flights()
        response = self.start()
        with patch("jobs.scrape_flightplan.fetch_flightplan", side_effect=ValueError("bad page")):
            self.execute_worker()
        status = self.client.get(
            f"/api/flightplan/refresh/{response.get_json()['run_id']}"
        ).get_json()["run"]
        self.assertEqual(status["status"], "failure")
        self.assertIn("bad page", status["error_summary"])
        self.assertEqual(self.flights(), before)

    def test_unexpected_worker_exception_is_persisted_and_logged(self):
        response = self.start()
        with patch.object(web, "scrape_enterprises", side_effect=RuntimeError("worker failed")):
            with patch.object(web.app.logger, "exception") as log:
                self.execute_worker()
        log.assert_called_once()
        status = self.client.get(
            f"/api/flightplan/refresh/{response.get_json()['run_id']}"
        ).get_json()["run"]
        self.assertEqual(status["status"], "failure")
        self.assertIn("worker failed", status["error_summary"])

    def test_thread_start_failure_releases_the_lease(self):
        self.thread.return_value.start.side_effect = RuntimeError("no thread")
        with patch.object(web.app.logger, "exception"):
            self.assertEqual(self.start().status_code, 503)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM import_runs").fetchone()[0], "failure")
        self.assertIsNotNone(acquire_run(self.db_path, stale_after_seconds=7200))

    def test_cli_import_blocks_ui_import(self):
        self.assertIsNotNone(acquire_run(self.db_path, stale_after_seconds=7200))
        self.assertEqual(self.start().status_code, 409)
        self.thread.assert_not_called()

    def test_unknown_inactive_and_invalid_airlines_are_rejected(self):
        self.assertEqual(self.start(999).status_code, 404)
        self.assertEqual(self.start(0).status_code, 400)
        self.assertEqual(self.start(9223372036854775808).status_code, 400)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("UPDATE airlines SET is_active=0 WHERE enterprise_id=80")
        self.assertEqual(self.start().status_code, 404)
        self.thread.assert_not_called()

    def test_update_requires_post_with_application_header(self):
        path = "/api/flightplan/airlines/80/refresh"
        self.assertEqual(self.client.get(path).status_code, 405)
        self.assertEqual(self.client.post(path).status_code, 403)
        self.thread.assert_not_called()

    def test_missing_database_and_unknown_run_are_reported(self):
        self.assertEqual(self.client.get("/api/flightplan/refresh/1").status_code, 404)
        with patch.object(web, "DATABASE_PATH", self.db_path.with_name("missing.sqlite3")):
            self.assertEqual(self.start().status_code, 503)
            self.assertEqual(self.client.get("/api/flightplan/refresh/1").status_code, 404)
        self.thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()

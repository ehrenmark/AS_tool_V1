import argparse
import base64
import io
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.security import check_password_hash, generate_password_hash

import app as web
from jobs.refresh_flightplans import acquire_run, refresh_all_data, run_refresh
from jobs.scrape_airports import AirportImportError, scrape_airports
from jobs.scrape_flightplan import (
    DEFAULT_BASE_URL, FlightplanSnapshot, ScrapeResult, connect_to_sqlite,
    ensure_table, fetch_enterprise_owner, scrape_all_enterprises,
    store_enterprise_owner, store_flightplan,
)


class AdminPlayersTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "test.sqlite3"
        self.path_patch = patch.object(web, "DATABASE_PATH", self.db_path)
        self.path_patch.start()
        self.config_patch = patch.dict(web.app.config, {
            "ADMIN_USERNAME": "owner",
            "ADMIN_PASSWORD_HASH": generate_password_hash("test-only-password", method="pbkdf2:sha256:1000"),
        })
        self.config_patch.start()
        self.client = web.app.test_client()
        self.headers = {
            "Authorization": "Basic " + base64.b64encode(b"owner:test-only-password").decode(),
            "X-Requested-With": "XMLHttpRequest",
        }
        with closing(connect_to_sqlite(self.db_path)) as connection:
            ensure_table(connection)
            for enterprise_id in (1, 2, 3, 4):
                store_flightplan(connection, enterprise_id, [{
                    "enterprise_id": enterprise_id, "enterprise_name": f"Air {enterprise_id}",
                    "flight_number": f"A{enterprise_id}", "frequency": "1234567",
                    "departure_time": "10:00", "arrival_time": "12:00",
                    "origin_airport_id": 1, "destination_airport_id": 2,
                    "source_url": DEFAULT_BASE_URL,
                }])
                store_enterprise_owner(connection, enterprise_id, {
                    "user_id": 27 if enterprise_id < 4 else 28,
                    "username": "Player" if enterprise_id < 4 else "Other",
                    "source_url": DEFAULT_BASE_URL + "/app/info/users/27",
                })
            connection.execute(
                "CREATE TABLE airports (airport_id INTEGER PRIMARY KEY, latitude REAL, longitude REAL)"
            )
            connection.executemany("INSERT INTO airports VALUES (?, ?, ?)", [(1, 0, 0), (2, 10, 10)])
            connection.commit()

    def tearDown(self):
        self.config_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def test_plain_admin_password_is_hashed_and_allows_login(self):
        with patch.dict("os.environ", {
            "AS_ADMIN_USERNAME": "owner", "AS_ADMIN_PASSWORD": "test-only-password",
            "AS_ADMIN_PASSWORD_HASH": "",
        }):
            credentials = web.admin_credentials()
        self.assertNotEqual(credentials["ADMIN_PASSWORD_HASH"], "test-only-password")
        self.assertTrue(check_password_hash(credentials["ADMIN_PASSWORD_HASH"], "test-only-password"))
        with patch.dict(web.app.config, credentials):
            self.assertEqual(self.client.get("/admin", headers=self.headers).status_code, 200)
            self.assertEqual(self.client.get("/admin").status_code, 401)

    def test_explicit_hash_takes_precedence_and_missing_credentials_remain_disabled(self):
        with patch.dict("os.environ", {
            "AS_ADMIN_USERNAME": "owner", "AS_ADMIN_PASSWORD": "unused",
            "AS_ADMIN_PASSWORD_HASH": "explicit-hash",
        }):
            self.assertEqual(web.admin_credentials()["ADMIN_PASSWORD_HASH"], "explicit-hash")
        with patch.dict("os.environ", {
            "AS_ADMIN_USERNAME": "", "AS_ADMIN_PASSWORD": "", "AS_ADMIN_PASSWORD_HASH": "",
        }):
            with patch.dict(web.app.config, web.admin_credentials()):
                self.assertEqual(self.client.get("/admin", headers=self.headers).status_code, 503)

    def test_admin_requires_configured_correct_credentials_for_every_endpoint(self):
        for path, method in [
            ("/admin", "get"), ("/api/admin/refresh/scan", "post"),
            ("/api/admin/refresh/all", "post"), ("/api/admin/refresh/1", "get"),
        ]:
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Basic", response.headers["WWW-Authenticate"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        wrong = {"Authorization": "Basic " + base64.b64encode(b"owner:wrong").decode()}
        self.assertEqual(self.client.get("/admin", headers=wrong).status_code, 401)
        with patch.dict(web.app.config, {"ADMIN_PASSWORD_HASH": ""}):
            self.assertEqual(self.client.get("/admin", headers=self.headers).status_code, 503)
        response = self.client.get("/admin", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-mode="scan"', response.data)
        self.assertIn(b'data-mode="all"', response.data)
        self.assertIn(b'Neue Airlines einpflegen', response.data)
        self.assertIn(b'Alle Airlines aktualisieren', response.data)

    def test_admin_starts_worker_reports_run_and_shares_lock(self):
        with patch.object(web, "Thread") as thread:
            response = self.client.post("/api/admin/refresh/scan", headers=self.headers)
            self.assertEqual(response.status_code, 202)
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[2], "scan")
            run_id = response.get_json()["run_id"]
            result = ScrapeResult(1, 1, 0, 5, 2, [])
            with patch.object(web, "scrape_all_enterprises", return_value=result) as scraper:
                thread.call_args.kwargs["target"](*args)
            self.assertTrue(scraper.call_args.kwargs["new_only"])
            status = self.client.get(f"/api/admin/refresh/{run_id}", headers=self.headers)
            self.assertEqual(status.get_json()["run"]["status"], "success")
            acquire_run(self.db_path, 7200)
            self.assertEqual(self.client.post("/api/admin/refresh/all", headers=self.headers).status_code, 409)

    def test_all_mode_updates_all_airlines_without_airports_and_rejects_non_application_posts(self):
        headers = {"Authorization": self.headers["Authorization"]}
        self.assertEqual(self.client.post("/api/admin/refresh/all", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/refresh/all", headers=self.headers).status_code, 405)
        self.assertEqual(self.client.post("/api/admin/refresh/invalid", headers=self.headers).status_code, 400)
        with patch.object(web, "Thread") as thread:
            response = self.client.post("/api/admin/refresh/all", headers=self.headers)
            self.assertEqual(response.status_code, 202)
            with patch.object(web, "scrape_all_enterprises", return_value=ScrapeResult(0, 0, 0, 0, 0, [])) as scraper, \
                    patch("jobs.scrape_airports.scrape_airports") as airports:
                thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
            scraper.assert_called_once()
            self.assertNotIn("new_only", scraper.call_args.kwargs)
            airports.assert_not_called()

    def test_owner_migration_and_foreign_key(self):
        with closing(connect_to_sqlite(self.db_path)) as connection:
            ensure_table(connection)
            store_enterprise_owner(connection, 1, {
                "user_id": 28, "username": "Renamed", "source_url": "https://example.test/users/28",
            })
            self.assertEqual(connection.execute("SELECT user_id FROM airlines WHERE enterprise_id=1").fetchone()[0], 28)
            self.assertEqual(connection.execute("SELECT username FROM users WHERE user_id=28").fetchone()[0], "Renamed")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE airlines SET user_id=999 WHERE enterprise_id=1")
            connection.rollback()

    def test_existing_airline_schema_is_migrated_without_losing_data(self):
        path = self.db_path.with_name("legacy.sqlite3")
        with closing(connect_to_sqlite(path)) as connection:
            connection.execute(
                """CREATE TABLE airlines (
                enterprise_id INTEGER PRIMARY KEY, enterprise_name TEXT NOT NULL,
                source_url TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
                last_seen_at TEXT, last_attempt_at TEXT, last_success_at TEXT, last_error TEXT
                )"""
            )
            connection.execute("INSERT INTO airlines VALUES (1, 'Legacy', 'source', 1, NULL, NULL, NULL, NULL)")
            connection.commit()
            ensure_table(connection)
            ensure_table(connection)
            row = connection.execute("SELECT * FROM airlines").fetchone()
            self.assertEqual(row["enterprise_name"], "Legacy")
            self.assertIsNone(row["user_id"])
            self.assertEqual(connection.execute("PRAGMA foreign_key_list(airlines)").fetchone()["table"], "users")

    def test_admin_thread_start_failure_is_reported_and_releases_lock(self):
        with patch.object(web, "Thread") as thread, patch.object(web.app.logger, "exception") as log:
            thread.return_value.start.side_effect = RuntimeError("no thread")
            response = self.client.post("/api/admin/refresh/scan", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        log.assert_called_once()
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM import_runs").fetchone()[0], "failure")
        self.assertIsNotNone(acquire_run(self.db_path, 7200))

    def test_players_and_airlines_can_be_filtered(self):
        self.assertEqual(self.client.get("/api/players").get_json(), [
            {"user_id": 28, "username": "Other", "airline_count": 1},
            {"user_id": 27, "username": "Player", "airline_count": 3},
        ])
        airlines = self.client.get("/api/flightplan/airlines?user_id=27").get_json()
        self.assertEqual([a["enterprise_id"] for a in airlines], [1, 2, 3])
        self.assertTrue(all(a["username"] == "Player" for a in airlines))
        for path in ("/api/flightplan/airlines", "/api/flightplan/routes"):
            body = self.client.get(path + "?user_id=999").get_json()
            self.assertEqual(body if path.endswith("airlines") else body["features"], [])
            for query in ("user_id=0", "user_id=x", "user_id=1&user_id=2", "user_id=9223372036854775808"):
                self.assertEqual(self.client.get(path + "?" + query).status_code, 400)
        response = self.client.get("/api/flightplan/routes?user_id=27")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["features"][0]["properties"]["enterprise_ids"], [1, 2, 3])
        self.assertEqual(response.get_json()["metadata"]["user_id"], 27)
        self.assertEqual(self.client.get("/api/flightplan/routes?user_id=27&enterprise_id=1").status_code, 400)

    def test_scan_imports_only_new_flightplans_and_updates_players(self):
        directory = [{"enterprise_id": i, "enterprise_name": f"Air {i}"} for i in (1, 2, 3, 4, 5)]
        owner = {"user_id": 29, "username": "New", "source_url": DEFAULT_BASE_URL + "/app/info/users/29"}
        with patch("jobs.scrape_flightplan.discover_enterprises", return_value=directory), \
                patch("jobs.scrape_flightplan.fetch_enterprise_owner", return_value=owner), \
                patch("jobs.scrape_flightplan.fetch_flightplan", return_value=FlightplanSnapshot("New Air", [])) as fetch:
            result = scrape_all_enterprises(self.db_path, new_only=True, request_delay=0, session=Mock())
        self.assertEqual(result.enterprises_total, 1)
        self.assertEqual(fetch.call_args.args[2], 5)
        with closing(connect_to_sqlite(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM flightplan_flights").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT count(*) FROM airlines WHERE user_id=29").fetchone()[0], 5)

    def test_full_refresh_includes_airports_and_coordinates_and_surfaces_errors(self):
        with patch("jobs.refresh_flightplans.scrape_all_enterprises", return_value=ScrapeResult(1, 1, 0, 2, 1, [])), \
                patch("jobs.scrape_airports.scrape_airports", side_effect=AirportImportError("incomplete")) as airports, \
                patch("jobs.add_airport_coordinates.update_coordinates") as coordinates:
            result = run_refresh(self.db_path, stale_after_seconds=7200, scraper=refresh_all_data)
        airports.assert_called_once()
        coordinates.assert_called_once()
        self.assertIn("incomplete", result.failures[0])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM import_runs").fetchone()[0], "partial")

    def test_failed_owner_fetch_preserves_mapping_and_is_not_hidden_by_flightplan_success(self):
        directory = [{"enterprise_id": i, "enterprise_name": f"Air {i}"} for i in (1, 2, 3, 4)]
        with patch("jobs.scrape_flightplan.discover_enterprises", return_value=directory), \
                patch("jobs.scrape_flightplan.fetch_enterprise_owner", side_effect=ValueError("missing director")), \
                patch("jobs.scrape_flightplan.fetch_flightplan", return_value=FlightplanSnapshot("Air", [])):
            result = run_refresh(self.db_path, stale_after_seconds=7200,
                                 scraper=scrape_all_enterprises, session=Mock(), request_delay=0)
        self.assertEqual(len(result.failures), 4)
        with closing(connect_to_sqlite(self.db_path)) as connection:
            row = connection.execute("SELECT user_id, last_error FROM airlines WHERE enterprise_id=1").fetchone()
            self.assertEqual(row["user_id"], 27)
            self.assertIn("missing director", row["last_error"])
            self.assertEqual(connection.execute("SELECT status FROM import_runs").fetchone()[0], "partial")

    def test_airport_refresh_continues_after_errors_and_reports_incomplete_snapshot(self):
        from test_airport_scraper import airport_page

        path = self.db_path.with_name("airports.sqlite3")
        args = argparse.Namespace(db_path=str(path), discover=True, timeout=20,
                                  delay=0, user_agent="test", stop_after_missing=100)
        with patch("jobs.scrape_airports.discover_airport_ids", return_value=[1, 2, 3]), \
                patch("jobs.scrape_airports.fetch_airport_html",
                      side_effect=[airport_page(), "bad page", airport_page("Updated")]) as fetch, \
                patch("builtins.print"):
            with self.assertRaises(AirportImportError):
                scrape_airports(args)
        self.assertEqual(fetch.call_count, 3)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT airport_name FROM airports WHERE airport_id=3").fetchone()[0], "Updated")

    def test_airport_logging_handles_names_outside_windows_console_encoding(self):
        from test_airport_scraper import airport_page

        path = self.db_path.with_name("unicode-airport.sqlite3")
        args = argparse.Namespace(db_path=str(path), discover=False, start_id=1,
                                  end_id=1, timeout=20, delay=0, user_agent="test",
                                  stop_after_missing=100)
        output = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with patch("sys.stdout", output), \
                patch("jobs.scrape_airports.fetch_airport_html", return_value=airport_page("Bra\u0219ov")):
            scrape_airports(args)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT airport_name FROM airports").fetchone()[0], "Bra\u0219ov")
        output.close()

    def test_owner_parser_uses_director_not_news_links(self):
        session = Mock()
        session.get.return_value.url = DEFAULT_BASE_URL + "/app/info/enterprises/80"
        session.get.return_value.text = """
            <a href="../users/99">News author</a>
            <table><tr><td>Managing director</td><td><a href="../users/27">Player</a></td></tr></table>
        """
        owner = fetch_enterprise_owner(session, DEFAULT_BASE_URL, 80, 20)
        self.assertEqual(owner["user_id"], 27)
        self.assertEqual(owner["username"], "Player")
        session.get.return_value.text = "<h1>Login</h1>"
        with self.assertRaises(ValueError):
            fetch_enterprise_owner(session, DEFAULT_BASE_URL, 80, 20)


if __name__ == "__main__":
    unittest.main()

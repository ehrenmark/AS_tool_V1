import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from bs4 import BeautifulSoup

from jobs.export_service_profiles import (
    DEFAULT_LOGIN_URL,
    build_login_payload,
    export_service_profiles,
    extract_detail_page,
    extract_service_profiles,
    gameworld_base_url,
    login,
    normalize_airline_id,
    select_airline,
    select_login_form,
)


class FakeResponse:
    def __init__(self, url, text):
        self.url = url
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.post_data = None

    def get(self, url, timeout, params=None):
        if url == "https://example.test/login":
            return FakeResponse(url, """
                <form action="/authenticate"><input name="username" type="text">
                <input name="password" type="password"></form>
            """)
        if url == "https://example.test/profiles":
            return FakeResponse(url, """
                <table><tbody><tr><td>Economy Plus</td><td>750 km</td>
                <td><a href="/edit">Bearbeiten</a><a href="/info">Info</a></td></tr></tbody></table>
            """)
        return FakeResponse(url, """
            <html><title>Economy Plus</title><table><tr><th>Meal</th><td>Hot meal</td></tr></table>
            <input name="comfort" value="5"></html>
        """)

    def post(self, url, data=None, timeout=None, json=None):
        self.post_data = (url, data)
        return FakeResponse(url, "<html>Logged in</html>")


class ApiLoginResponse(FakeResponse):
    def json(self):
        return {"id": "session-id", "token": "signed-session-token"}


class ApiLoginSession:
    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.post_data = None

    def post(self, url, json, timeout):
        self.post_data = (url, json)
        return ApiLoginResponse(url, "")


class ServiceProfileExporterTests(unittest.TestCase):
    def test_login_payload_preserves_csrf_token(self):
        form = select_login_form(BeautifulSoup("""
            <form action="/authenticate">
              <input type="hidden" name="_csrf" value="token">
              <input type="text" name="username">
              <input type="password" name="password">
            </form>
        """, "html.parser"))

        self.assertEqual(
            build_login_payload(form, "pilot", "secret"),
            {"_csrf": "token", "username": "pilot", "password": "secret"},
        )

    def test_service_profile_cards_are_exported(self):
        profiles = extract_service_profiles("""
            <section class="service-profile">
              <h2>Economy Plus</h2><dl><dt>Cabin</dt><dd>Economy</dd></dl>
              <table><thead><tr><th>Meal</th><th>Rating</th></tr></thead>
                <tbody><tr><td>Hot meal</td><td>5</td></tr></tbody></table>
            </section>
        """)

        self.assertEqual(profiles, [{
            "name": "Economy Plus",
            "fields": {"Cabin": "Economy"},
            "records": [{"Meal": "Hot meal", "Rating": "5"}],
        }])

    def test_service_profile_table_is_exported_when_no_cards_exist(self):
        profiles = extract_service_profiles("""
            <table><thead><tr><th>Name</th><th>Class</th></tr></thead>
            <tbody><tr><td>Comfort</td><td>Business</td></tr></tbody></table>
        """)

        self.assertEqual(profiles[0]["records"], [{"Name": "Comfort", "Class": "Business"}])

    def test_central_login_sets_signed_token_for_gameworld(self):
        session = ApiLoginSession()

        login(
            session,
            DEFAULT_LOGIN_URL,
            "pilot",
            "secret",
            30,
            "https://quimby.airlinesim.aero/action/enterprise/serviceProfiles",
        )

        self.assertEqual(session.post_data[1]["method"], "password")
        self.assertEqual(
            session.cookies.get("as-sid", domain="quimby.airlinesim.aero", path="/"),
            "signed-session-token",
        )

    def test_export_logs_in_and_writes_json_without_credentials(self):
        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "profiles.json"
            session = FakeSession()

            export = export_service_profiles(
                output_path,
                username="pilot",
                password="secret",
                login_url="https://example.test/login",
                service_profiles_url="https://example.test/profiles",
                session=session,
            )

            self.assertEqual(session.post_data, (
                "https://example.test/authenticate",
                {"username": "pilot", "password": "secret"},
            ))
            self.assertEqual(export["profiles"][0]["name"], "Economy Plus")
            self.assertEqual(export["profiles"][0]["minimum_route"], "750 km")
            self.assertEqual(export["profiles"][0]["details"]["bearbeiten"]["fields"], {"Meal": "Hot meal"})
            self.assertEqual(export["profiles"][0]["details"]["info"]["form_values"], {"comfort": "5"})
            saved_json = output_path.read_text(encoding="utf-8")
            self.assertIn("Economy Plus", saved_json)
            self.assertNotIn("secret", saved_json)

    def test_detail_page_keeps_only_selected_radio_value(self):
        details = extract_detail_page("""
            <input type="radio" name="meal" value="none">
            <input type="radio" name="meal" value="hot" checked>
        """)

        self.assertEqual(details["form_values"], {"meal": "hot"})


if __name__ == "__main__":
    unittest.main()

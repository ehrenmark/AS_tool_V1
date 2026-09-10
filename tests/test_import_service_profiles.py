import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup

from jobs.import_service_profiles import change_payload, import_service_profiles, saved_form_values


class FakeResponse:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.headers = {}
        self.posts: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, timeout: int, params=None) -> FakeResponse:
        return FakeResponse(url, """
            <form method="post" action="serviceNewProfile"><input name="name"></form>
            <table><tr><td>Existing</td><td>100 km</td><td><a href="/edit">Bearbeiten</a></td></tr></table>
        """)

    def post(self, url: str, data=None, timeout=None, json=None) -> FakeResponse:
        self.posts.append((url, data or {}))
        return FakeResponse(url, "")


class ServiceProfileImporterTests(unittest.TestCase):
    def test_saved_form_values_excludes_global_search(self):
        profile = {"name": "Comfort", "details": {"bearbeiten": {"form_values": {"query": "", "dry": "2"}}}}

        self.assertEqual(saved_form_values(profile), {"dry": "2"})

    def test_change_payload_keeps_id_and_validates_radio_choice(self):
        form = BeautifulSoup("""
            <form action="serviceChange">
              <input type="hidden" name="id" value="42">
              <input type="radio" name="dry" value="1">
              <input type="radio" name="dry" value="2">
            </form>
        """, "html.parser").form

        self.assertEqual(change_payload(form, {"dry": "2", "query": ""}, "Comfort"), {"id": "42", "dry": "2"})
        with self.assertRaisesRegex(ValueError, "nicht verfügbar"):
            change_payload(form, {"dry": "3"}, "Comfort")

    def test_preview_never_submits_create_requests_and_skips_existing(self):
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "profiles.json"
            input_path.write_text(json.dumps({"profiles": [
                {"name": "Existing", "details": {}},
                {"name": "New", "details": {}},
            ]}), encoding="utf-8")
            session = FakeSession()

            with patch("jobs.import_service_profiles.login"):
                result = import_service_profiles(
                    input_path,
                    username="pilot",
                    password="secret",
                    login_url="https://example.test/login",
                    service_profiles_url="https://example.test/profiles",
                    session=session,
                )

        self.assertEqual(result, {"created": [], "skipped_existing": ["Existing"], "would_create": ["New"]})
        self.assertEqual(session.posts, [])


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bs4 import BeautifulSoup

from jobs.import_cabins import create_payload, editor_payload, import_cabins, load_configurations


class FakeResponse:
    def __init__(self, url, text):
        self.url, self.text = url, text

    def raise_for_status(self):
        return None


class CabinSession:
    def __init__(self, existing=False):
        self.headers = {}
        self.posts = []
        self.existing = existing
        self.created = False
        self.classes = []
        self.values = {}

    def overview(self):
        row = '<tr class="subhead"><td>Airbus A321neo light (1)</td></tr><tr><td>KaiTak1</td><td><a href="/editor">Edit</a></td></tr>' if (self.existing or self.created) else ''
        return '''<form method="post" action="create~any"><input name="name"><select name="type"><option value="a321">Airbus A321neo light</option></select></form><table>%s</table>''' % row

    def editor(self):
        cabins = ''.join('<div class="cabin"><h3>%s</h3><input name="editor:deck:cabins:%s:fas" value="%s"></div>' % ({"F": "First Class (F)", "C": "Business Class (C)", "Y": "Economy Class (Y)"}[code], index, self.values.get("editor:deck:cabins:%s:fas" % index, "")) for index, code in enumerate(self.classes))
        return '''<form method="post" action="./2033?1-1.-editor~form"><input type="hidden" name="id" value="42"><input name="summary:name" value="%s">%s<script>Wicket.Ajax.ajax({"u":"./2033?1-1.0-editor~form-editor-deck-cabins~add-cabins","m":"POST"});</script></form>''' % (self.values.get("summary:name", ""), cabins)

    def get(self, url, timeout, params=None):
        return FakeResponse(url, self.editor() if "/editor" in url or "2033" in url else self.overview())

    def post(self, url, data=None, timeout=None, json=None, headers=None):
        data = data or {}
        self.posts.append((url, data, headers))
        if "cabins~add-cabins" in url:
            self.classes.append(data["editor:deck:cabins-add:cabins"])
            return FakeResponse(url, "<ajax-response/>")
        if "create~any" in url:
            self.created = True
            return FakeResponse("https://example.test/editor", self.editor())
        self.values.update(data)
        return FakeResponse(url, self.editor())


class DelayedCabinSession(CabinSession):
    def __init__(self, existing=False):
        super().__init__(existing)
        self.stale_editor = None

    def get(self, url, timeout, params=None):
        if self.stale_editor is not None and ("/editor" in url or "2033" in url):
            stale_editor, self.stale_editor = self.stale_editor, None
            return FakeResponse(url, stale_editor)
        return super().get(url, timeout, params)

    def post(self, url, data=None, timeout=None, json=None, headers=None):
        is_editor_submit = "cabins~add-cabins" not in url and "create~any" not in url
        if is_editor_submit:
            self.stale_editor = self.editor()
        return super().post(url, data, timeout, json, headers)


class CabinImporterTests(unittest.TestCase):
    def configuration(self):
        return {"name": "KaiTak1", "aircraft_type": "Airbus A321neo light", "details": {
            "form_values": {"summary:name": "KaiTak1", "editor:deck:cabins:0:fas": "1", "editor:deck:cabins:1:fas": "2", "editor:deck:cabins:2:fas": "3"},
            "cabins": [{"class": "First Class (F)"}, {"class": "Business Class (C)"}, {"class": "Economy Class (Y)"}],
        }}

    def write_export(self, directory):
        path = Path(directory) / "cabins.json"
        path.write_text(json.dumps({"configurations": [self.configuration()]}), encoding="utf-8")
        return path

    def test_load_configurations_rejects_duplicate_names(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cabins.json"
            path.write_text(json.dumps({"configurations": [self.configuration(), self.configuration()]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "demselben Namen"):
                load_configurations(path)

    def test_create_and_editor_payload(self):
        form = BeautifulSoup('<form><input name="name"><select name="type"><option value="a321">Airbus A321neo light</option></select></form>', "html.parser").form
        self.assertEqual(create_payload(form, "KaiTak1", "Airbus A321neo light"), {"name": "KaiTak1", "type": "a321"})
        editor = BeautifulSoup('<form><input type="hidden" name="id" value="42"><input name="summary:name"></form>', "html.parser").form
        self.assertEqual(editor_payload(editor, {"summary:name": "KaiTak1", "searchQuery": "x"}, "KaiTak1"), {"id": "42", "summary:name": "KaiTak1"})

    def test_preview_reports_existing_update_without_writes(self):
        with TemporaryDirectory() as directory, patch("jobs.import_cabins.login"):
            session = CabinSession(existing=True)
            result = import_cabins(self.write_export(directory), username="pilot", password="secret", login_url="https://example.test/login", cabins_url="https://example.test/seating", session=session)
        self.assertEqual(result, {"created": [], "updated_existing": [], "skipped_existing": [], "would_create": [], "would_update": ["KaiTak1"]})
        self.assertEqual(session.posts, [])

    def test_apply_creates_classes_before_submitting_all_settings(self):
        with TemporaryDirectory() as directory, patch("jobs.import_cabins.login"):
            session = CabinSession()
            result = import_cabins(self.write_export(directory), username="pilot", password="secret", login_url="https://example.test/login", cabins_url="https://example.test/seating", apply=True, session=session)
        self.assertEqual(result, {"created": ["KaiTak1"], "updated_existing": [], "skipped_existing": [], "would_create": [], "would_update": []})
        self.assertEqual(session.classes, ["F", "C", "Y"])
        final_payload = session.posts[-1][1]
        self.assertEqual(final_payload["summary:name"], "KaiTak1")
        self.assertEqual([final_payload["editor:deck:cabins:%s:fas" % i] for i in range(3)], ["1", "2", "3"])
        self.assertEqual(session.posts[-1][0], "https://example.test/2033?1-1.-editor~form")

    def test_apply_updates_blank_existing_cabin_without_duplicate_classes(self):
        with TemporaryDirectory() as directory, patch("jobs.import_cabins.login"):
            session = CabinSession(existing=True)
            session.classes = ["F"]
            result = import_cabins(self.write_export(directory), username="pilot", password="secret", login_url="https://example.test/login", cabins_url="https://example.test/seating", apply=True, session=session)
        self.assertEqual(result, {"created": [], "updated_existing": ["KaiTak1"], "skipped_existing": [], "would_create": [], "would_update": []})
        self.assertEqual(session.classes, ["F", "C", "Y"])
        self.assertEqual(sum("cabins~add-cabins" in post[0] for post in session.posts), 2)

    def test_apply_retries_when_saved_editor_is_initially_stale(self):
        with TemporaryDirectory() as directory, patch("jobs.import_cabins.login"), patch("jobs.import_cabins.time.sleep") as sleep:
            session = DelayedCabinSession(existing=True)
            result = import_cabins(self.write_export(directory), username="pilot", password="secret", login_url="https://example.test/login", cabins_url="https://example.test/seating", apply=True, session=session)
        self.assertEqual(result["updated_existing"], ["KaiTak1"])
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()

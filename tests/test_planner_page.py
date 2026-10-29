import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from werkzeug.datastructures import MultiDict

from app import app


ROOT = Path(__file__).resolve().parents[1]


class PlannerPageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.data = {
            'base': 'FRA', 'start_day': '0', 'start_time': '06:00', 'ground_minutes': '45',
            'destination': ['LHR', 'CDG'], 'outbound': ['90', '60'],
            'inbound': ['100', '75'], 'frequency': ['7', '2'],
        }

    def test_form_renders_plan_without_database_or_external_writes(self):
        with patch('app.connect_database') as connect:
            response = self.client.post('/flugplaene', data=self.data)
        self.assertEqual(response.status_code, 200)
        connect.assert_not_called()
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertIn('Alle 9 gewünschten Umläufe', page.text)
        self.assertEqual(len(page.select('.planner-table tbody tr')), 18)
        self.assertEqual(len(page.select('.planner-day')), 7)
        self.assertEqual(len(page.select('.planner-flight')), 18)
        self.assertEqual(page.select_one('[name="base"]')['value'], 'FRA')
        self.assertEqual(len(page.select('#plannerRoutes .planner-route')), 2)
        self.assertIsNotNone(page.select_one('#plannerStale[hidden]'))

    def test_invalid_input_displays_error_and_retains_values(self):
        self.data['outbound'] = ['0', '60']
        response = self.client.post('/flugplaene', data=self.data)
        self.assertEqual(response.status_code, 400)
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertIn('Hinflugzeit', page.select_one('[role="alert"]').text)
        self.assertEqual(page.select_one('[name="outbound"]')['value'], '0')
        self.assertIsNone(page.select_one('.planner-table'))

    def test_incomplete_request_is_rejected(self):
        for data in ({}, {**self.data, 'inbound': ['90']},
                     {**self.data, 'frequency': []},
                     MultiDict([('destination', 'LHR')] * 21)):
            with self.subTest(data=data):
                response = self.client.post('/flugplaene', data=data)
                self.assertEqual(response.status_code, 400)
                self.assertIn('role="alert"', response.text)

    def test_too_many_routes_is_rejected(self):
        data = {**self.data, **{field: ['LHR' if field == 'destination' else '1'] * 21
                              for field in ('destination', 'outbound', 'inbound', 'frequency')}}
        response = self.client.post('/flugplaene', data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn('höchstens 20', response.text)

    def test_unfulfilled_request_is_explicit_partial_preview(self):
        response = self.client.post('/flugplaene', data={
            **self.data, 'start_day': '6', 'start_time': '23:59',
        })
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertIn('Nicht alle Vorgaben passen', page.select_one('[role="alert"]').text)
        self.assertIn('LHR: 7 Umläufe nicht eingeplant', page.text)
        self.assertIn('CDG: 2 Umläufe nicht eingeplant', page.text)
        self.assertIn('Kein vollständiger Hin-/Rückflug', page.text)
        self.assertIsNone(page.select_one('.planner-success'))

    def test_reflected_values_are_html_escaped(self):
        response = self.client.post('/flugplaene', data={
            **self.data, 'base': '"><script>alert(1)</script>',
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('<script>alert(1)</script>', response.text)
        page = BeautifulSoup(response.data, 'html.parser')
        self.assertEqual(page.select_one('[name="base"]')['value'], '"><script>alert(1)</script>')

    def test_route_controls_and_stale_preview_behavior(self):
        result = subprocess.run(
            ['node', str(ROOT / 'tests' / 'planner_ui.cjs')],
            cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8',
        )
        self.assertIn('planner UI: OK', result.stdout)


if __name__ == '__main__':
    unittest.main()

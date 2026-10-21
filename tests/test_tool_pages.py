import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from app import app


class ToolPageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.data, 'html.parser')

    def test_landing_page_links_to_both_tools_without_loading_map(self):
        with patch('app.connect_database') as connect:
            page = self.page('/')
        connect.assert_not_called()
        cards = page.select('.tool-card')
        self.assertEqual([card.h2.text for card in cards], ['Map', 'Flugpläne'])
        self.assertEqual([card['href'] for card in cards], ['/map', '/flugplaene'])
        self.assertIn('In Planung', cards[1].text)
        self.assertIsNone(page.select_one('#map'))
        self.assertFalse(page.find_all('script'))

    def test_map_keeps_dashboard_and_links_back_to_tool_selection(self):
        page = self.page('/map?airlines=80')
        self.assertIsNotNone(page.select_one('#map'))
        self.assertIsNotNone(page.select_one('#airlineSelect'))
        self.assertIsNotNone(page.select_one('#updateAirlineButton'))
        self.assertEqual(page.select_one('.tool-back')['href'], '/')
        scripts = [script.get('src', '') for script in page.find_all('script')]
        self.assertIn('/static/js/app.js', scripts)
        self.assertIn('/static/js/demand-filters.js', scripts)

    def test_flightplans_page_is_explicit_placeholder_with_navigation(self):
        page = self.page('/flugplaene')
        self.assertEqual(page.h1.text, 'Flugpläne')
        self.assertIn('noch nicht verfügbar', page.text)
        self.assertIsNotNone(page.select_one('a[href="/"]'))
        self.assertIsNotNone(page.select_one('a[href="/map"]'))
        self.assertFalse(page.find_all('script'))


if __name__ == '__main__':
    unittest.main()

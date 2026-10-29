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

    def test_landing_page_links_to_map_and_disables_flightplans_without_loading_map(self):
        with patch('app.connect_database') as connect:
            page = self.page('/')
        connect.assert_not_called()
        cards = page.select('.tool-card')
        self.assertEqual([card.h2.text for card in cards], ['Map', 'Flugpläne'])
        self.assertEqual(cards[0]['href'], '/map')
        self.assertEqual(cards[1].name, 'div')
        self.assertIn('tool-card-disabled', cards[1]['class'])
        self.assertEqual(cards[1]['aria-disabled'], 'true')
        self.assertEqual(cards[1]['aria-labelledby'], cards[1].h2['id'])
        self.assertFalse(cards[1].has_attr('href'))
        self.assertFalse(cards[1].has_attr('tabindex'))
        self.assertIsNone(cards[1].select_one('a, button, input'))
        self.assertIsNone(page.select_one('a[href="/flugplaene"]'))
        self.assertIn('Demnächst verfügbar', cards[1].text)
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

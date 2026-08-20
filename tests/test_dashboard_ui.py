import unittest
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DashboardUiTests(unittest.TestCase):
    def run_filter_helpers(self, expression):
        script = (
            "const h=require('./static/js/demand-filters.js');"
            f"console.log(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            ['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_dashboard_has_no_weekday_control_or_request_parameter(self):
        template = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        self.assertNotIn('weekdaySelect', template)
        self.assertNotIn('weekdaySelect', script)
        self.assertNotIn("params.set('weekday'", script)

    def test_dashboard_has_no_time_or_manual_refresh_controls(self):
        template = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        for control in ('fixedTimeToggle', 'timeInput', 'resetCurrent', 'refreshButton', 'importStatus'):
            self.assertNotIn(control, template)
            self.assertNotIn(control, script)
        self.assertNotIn("params.set('time'", script)

    def test_airport_details_separate_upcoming_flights(self):
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        self.assertIn("renderFlightSection('Nächste Abflüge', 'Abflug', departing, 'next_departure_at', 'departure_time')", script)
        self.assertIn("renderFlightSection('Nächste Ankünfte', 'Ankunft', arriving, 'next_arrival_at', 'arrival_time')", script)

    def test_initial_state_does_not_load_all_flights(self):
        template = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        self.assertIn("new Option('Airline auswählen', '')", script)
        self.assertIn('if (!elements.airlineSelect.value)', script)
        self.assertIn('if (elements.airlineSelect.value) await loadDashboard(); else clearDashboard();', script)
        self.assertIn('Airline auswählen, um den Flugplan anzuzeigen.', template)

    def test_all_airlines_is_explicit_first_bold_selection(self):
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        styles = (ROOT / 'static' / 'css' / 'style.css').read_text(encoding='utf-8')

        all_option = script.index("new Option('Alle Airlines', ALL_AIRLINES)")
        airline_loop = script.index('airlines.forEach((airline) =>')
        self.assertLess(all_option, airline_loop)
        self.assertIn("allAirlines.className = 'all-airlines-option'", script)
        self.assertIn("classList.toggle('all-airlines-selected'", script)
        self.assertIn('.field select option.all-airlines-option, .field select.all-airlines-selected { font-weight: 800; }', styles)

    def test_map_uses_one_click_handler_for_overlapping_layers(self):
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        self.assertEqual(script.count("map.on('click'"), 1)
        self.assertIn('map.queryRenderedFeatures(event.point', script)
        self.assertNotIn("map.on('click', layer", script)

    def test_longest_routes_mark_combined_directions(self):
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')

        self.assertIn("route.bidirectional ? '↔' : '→'", script)

    def test_group_toggle_selects_all_or_none_from_actual_state(self):
        self.assertEqual(self.run_filter_helpers(
            "[...h.toggledLevels([0,1,2],new Set([0,1,2]))]"), [])
        self.assertEqual(self.run_filter_helpers(
            "[...h.toggledLevels([0,1,2],new Set([0]))]"), [0, 1, 2])

    def test_empty_group_filters_its_layer_and_destination_overlay(self):
        hidden = ['==', 1, 0]
        self.assertEqual(self.run_filter_helpers(
            "h.layerFilter('passenger_demand',new Set())"), hidden)
        destination = self.run_filter_helpers(
            "h.destinationFilter(new Set(),new Set([0,1]))")
        self.assertEqual(destination[0], 'all')
        self.assertIn(hidden, destination)

    def test_toggle_buttons_are_explicit_and_destination_semantics_are_visible(self):
        template = (ROOT / 'templates' / 'index.html').read_text(encoding='utf-8')
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="togglePassenger"', template)
        self.assertIn('id="toggleCargo"', template)
        self.assertIn('nach Aufkommen gefiltert', template)
        self.assertIn("elements.togglePassenger.addEventListener('click'", script)
        self.assertIn("elements.toggleCargo.addEventListener('click'", script)

    def test_empty_permalink_selection_does_not_restore_level_zero(self):
        script = (ROOT / 'static' / 'js' / 'app.js').read_text(encoding='utf-8')
        self.assertIn("filter((value) => value !== '').map(Number)", script)


if __name__ == '__main__':
    unittest.main()

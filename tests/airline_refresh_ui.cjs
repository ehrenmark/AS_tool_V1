'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(fetch) {
    const nodes = new Map();
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            value: '', options: [], disabled: false, textContent: '', className: '',
            classList: { toggle() {} },
            setAttribute(key, value) { this[key] = value; },
            addEventListener() {},
            add(option) { this.options.push(option); },
            replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; }
        });
        return nodes.get(id);
    }
    const context = vm.createContext({
        console, URLSearchParams, AbortController,
        location: { search: '?airlines=999,888' },
        document: {
            getElementById: node, addEventListener() {},
            createElement() {
                return { children: [], appendChild(child) { this.children.push(child); } };
            }
        },
        mapboxgl: {
            Map: class { addControl() {} on() {} getSource() {} },
            NavigationControl: class {}, FullscreenControl: class {}
        },
        Option: class { constructor(text, value) { this.text = text; this.value = value; } },
        setTimeout(callback) { queueMicrotask(callback); return 1; },
        requestAnimationFrame() {},
        fetch, dashboardReloads: 0
    });
    vm.runInContext(fs.readFileSync(path.join('static', 'js', 'app.js'), 'utf8'), context);
    vm.runInContext('loadDashboard = async () => { globalThis.dashboardReloads += 1; };', context);
    const api = vm.runInContext(
        '({ elements, populateAirlines, syncComparisonOptions, updateSelectedAirline, popupNode })', context
    );
    api.populateAirlines([
        { enterprise_id: 80, enterprise_name: 'Alpha', flight_count: 1 },
        { enterprise_id: 90, enterprise_name: 'Beta', flight_count: 2 }
    ]);
    api.elements.airlineSelect.value = '80';
    api.elements.comparisonSelect.value = '90';
    api.syncComparisonOptions();
    return { context, ...api };
}

function response(body, status = 200) {
    return { ok: status >= 200 && status < 300, status, async json() { return body; } };
}

const catalog = [
    { enterprise_id: 80, enterprise_name: 'Alpha renamed', flight_count: 3 },
    { enterprise_id: 90, enterprise_name: 'Beta', flight_count: 2 }
];

async function main() {
    {
        const api = setup(async () => {});
        const now = Date.now();
        const feature = arrival => ({
            geometry: { type: 'Point', coordinates: [0, 0] },
            properties: {
                departure_at: new Date(now - 60_000).toISOString(),
                arrival_at: new Date(arrival).toISOString(),
                origin_coordinates: [0, 0], destination_coordinates: [1, 1]
            }
        });
        vm.runInContext(`aircraftData = ${JSON.stringify({
            type: 'FeatureCollection', features: [feature(now - 1000), feature(now + 60_000)]
        })}; updateAircraftState();`, api.context);
        assert.equal(api.elements.aircraftState.textContent, '2 Flüge laut Flugplan unterwegs');
        vm.runInContext('animateAircraftFrame(1000);', api.context);
        assert.equal(api.elements.aircraftState.textContent, '1 Flug laut Flugplan unterwegs');
        vm.runInContext(`aircraftData.features[0].properties.arrival_at = '${new Date(now - 1000).toISOString()}';
            animateAircraftFrame(2000);`, api.context);
        assert.equal(api.elements.aircraftState.textContent, 'Keine Flüge laut Flugplan unterwegs.');
    }
    {
        const api = setup(async () => {});
        const popup = api.popupNode({
            flight_number: 'AA 1', origin_iata: 'AAA', destination_iata: 'BBB',
            departure_time: '19:30', arrival_time: '06:15', aircraft_type: '320',
            departure_at: '2026-10-21T19:30:00-04:00',
            arrival_at: '2026-10-22T06:15:00+02:00'
        }, 'aircraft');
        const times = popup.children[2].textContent;
        assert.match(times, /Mi[.,]/);
        assert.match(times, /Do[.,]/);
        assert.match(times, /23:30 UTC/);
        assert.match(times, /04:15 UTC/);
        assert.doesNotMatch(times, /19:30|06:15/);
        const unknown = api.popupNode({
            departure_time: '19:30', arrival_time: '06:15', aircraft_type: '320'
        }, 'aircraft');
        assert.doesNotMatch(unknown.children[2].textContent, /19:30|06:15|UTC/);
        assert.match(unknown.children[2].textContent, /320/);
    }
    {
        let calls = 0;
        const api = setup(async () => { calls += 1; });
        for (const value of ['', 'all']) {
            api.elements.airlineSelect.value = value;
            api.syncComparisonOptions();
            assert.equal(api.elements.updateAirlineButton.disabled, true);
            await api.updateSelectedAirline();
        }
        assert.equal(calls, 0);
    }
    {
        const calls = [];
        const replies = [
            response({ run_id: 7 }, 202),
            response({ run: { status: 'running' } }),
            response({ run: { status: 'success', flights_imported: 3 } }),
            response(catalog)
        ];
        const api = setup(async (path, options) => {
            calls.push({ path, options });
            return replies.shift();
        });
        await api.updateSelectedAirline();
        assert.equal(calls[0].path, '/api/flightplan/airlines/80/refresh');
        assert.equal(calls[0].options.method, 'POST');
        assert.equal(calls[0].options.headers['X-Requested-With'], 'XMLHttpRequest');
        assert.equal(calls[1].path, '/api/flightplan/refresh/7');
        assert.equal(calls[2].path, '/api/flightplan/refresh/7');
        assert.equal(calls[3].path, '/api/flightplan/airlines');
        assert.equal(api.context.dashboardReloads, 1);
        assert.equal(api.elements.airlineSelect.value, '80');
        assert.equal(api.elements.comparisonSelect.value, '90');
        assert.equal(api.elements.airlineSelect.options.find(option => option.value === '80').text,
            'Alpha renamed (3 Flüge)');
        assert.equal(api.elements.updateAirlineButton.disabled, false);
        assert.equal(api.elements.airlineSelect.disabled, false);
        assert.equal(api.elements.comparisonSelect.disabled, false);
        assert.match(api.elements.airlineUpdateState.textContent, /3 Einträge/);
        assert.match(api.elements.airlineUpdateState.className, /success/);
    }
    {
        let release;
        let calls = 0;
        const api = setup(async () => {
            calls += 1;
            if (calls === 1) return new Promise(resolve => { release = resolve; });
            if (calls === 2) return response({ run: { status: 'success', flights_imported: 3 } });
            return response(catalog);
        });
        const pending = api.updateSelectedAirline();
        assert.equal(api.elements.airlineSelect.disabled, true);
        assert.equal(api.elements.comparisonSelect.disabled, true);
        assert.equal(api.elements.updateAirlineButton.disabled, true);
        await api.updateSelectedAirline();
        assert.equal(calls, 1);
        release(response({ run_id: 7 }, 202));
        await pending;
    }
    {
        const replies = [
            response({ run_id: 7 }, 202),
            response({ run: { status: 'failure', error_summary: 'Invalid flightplan' } })
        ];
        const api = setup(async () => replies.shift());
        await api.updateSelectedAirline();
        assert.match(api.elements.airlineUpdateState.textContent, /Invalid flightplan/);
        assert.match(api.elements.airlineUpdateState.className, /error/);
        assert.equal(api.context.dashboardReloads, 0);
        assert.equal(api.elements.updateAirlineButton.disabled, false);
    }
    {
        const api = setup(async () => response({ error: 'Already running' }, 409));
        await api.updateSelectedAirline();
        assert.equal(api.elements.airlineUpdateState.textContent, 'Already running');
        assert.equal(api.context.dashboardReloads, 0);
        assert.equal(api.elements.airlineSelect.disabled, false);
    }
    {
        let calls = 0;
        const api = setup(async () => {
            calls += 1;
            if (calls === 1) return response({ run_id: 7 }, 202);
            throw new Error('offline');
        });
        await api.updateSelectedAirline();
        assert.match(api.elements.airlineUpdateState.textContent, /offline/);
        assert.match(api.elements.airlineUpdateState.textContent, /Hintergrund weiterlaufen/);
        assert.equal(api.context.dashboardReloads, 0);
    }
    {
        const replies = [
            response({ run_id: 7 }, 202),
            response({ run: { status: 'success', flights_imported: 3 } }),
            response({}, 503)
        ];
        const api = setup(async () => replies.shift());
        await api.updateSelectedAirline();
        assert.match(api.elements.airlineUpdateState.textContent, /Update erfolgreich, aber/);
        assert.match(api.elements.airlineUpdateState.className, /error/);
    }
    {
        let calls = 0;
        const api = setup(async () => {
            calls += 1;
            return response(calls === 1 ? { run_id: 7 } : { run: { status: 'running' } }, 200);
        });
        await api.updateSelectedAirline();
        assert.equal(calls, 401);
        assert.match(api.elements.airlineUpdateState.textContent, /Zeitlimit/);
        assert.match(api.elements.airlineUpdateState.textContent, /Hintergrund weiterlaufen/);
    }
    console.log('airline refresh UI: OK');
}

main().catch(error => { console.error(error); process.exitCode = 1; });

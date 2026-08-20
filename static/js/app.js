'use strict';

mapboxgl.accessToken = 'pk.eyJ1IjoibWFya21vZGUiLCJhIjoiY21zODBnbTkyMDF0ZDM1c2c1dTN6OXEzNSJ9.MMJEVkIXt_h0HSnjAlN8uA';

const EMPTY_GEOJSON = () => ({ type: 'FeatureCollection', features: [] });
const ALL_AIRLINES = 'all';
const DEMAND_LEVELS = Array.from({ length: 11 }, (_, index) => index);
const selectedPassengerLevels = new Set(DEMAND_LEVELS);
const selectedCargoLevels = new Set(DEMAND_LEVELS);
const $ = (id) => document.getElementById(id);
const elements = {
    passengerFilters: $('passengerFilters'), cargoFilters: $('cargoFilters'), airportCount: $('airportCount'),
    togglePassenger: $('togglePassenger'), toggleCargo: $('toggleCargo'),
    destinationState: $('destinationState'), aircraftState: $('aircraftState'), routeState: $('routeState'), kpiState: $('kpiState'),
    routeToggle: $('routeToggle'), aircraftToggle: $('aircraftToggle'), airlineSelect: $('airlineSelect'),
    comparisonSelect: $('comparisonSelect'), comparisonLegend: $('comparisonLegend'),
    kpiGrid: $('kpiGrid'), longestRoutes: $('longestRoutes'),
    drawer: $('airportDrawer'), drawerClose: $('drawerClose'),
    drawerTitle: $('drawerTitle'), drawerContent: $('drawerContent'), drawerState: $('drawerState')
};

let airportData = EMPTY_GEOJSON();
let routeData = EMPTY_GEOJSON();
let destinationData = EMPTY_GEOJSON();
let aircraftData = EMPTY_GEOJSON();
let dataRequestId = 0;
let detailRequestId = 0;
let permalinkTimer;
let mapReady = false;
let selectedAirportId = '';
let selectedAirportProperties = {};

const initialParams = new URLSearchParams(location.search);
const initialCamera = {
    center: parsePair(initialParams.get('center')) || [10, 30],
    zoom: validNumber(initialParams.get('zoom'), 1.6),
    bearing: validNumber(initialParams.get('bearing'), 0),
    pitch: validNumber(initialParams.get('pitch'), 0)
};

const map = new mapboxgl.Map({
    container: 'map', style: 'mapbox://styles/mapbox/dark-v11', projection: 'globe', ...initialCamera
});
map.addControl(new mapboxgl.NavigationControl(), 'top-right');
map.addControl(new mapboxgl.FullscreenControl(), 'top-right');
map.on('style.load', () => map.setFog({ color: 'rgb(186, 210, 235)', 'high-color': 'rgb(36, 92, 223)', 'horizon-blend': 0.02, 'space-color': 'rgb(11, 11, 25)', 'star-intensity': 0.6 }));

function validNumber(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
}

function parsePair(value) {
    if (!value) return null;
    const pair = value.split(',').map(Number);
    if (pair.length !== 2 || !pair.every(Number.isFinite)) return null;
    const [longitude, latitude] = pair;
    return longitude >= -180 && longitude <= 180 && latitude >= -90 && latitude <= 90 ? pair : null;
}

function text(value, fallback = '–') {
    return value === null || value === undefined || value === '' ? fallback : String(value);
}

function numberText(value) {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat('de-DE').format(number) : text(value);
}

function setState(element, message, kind = '') {
    element.textContent = message;
    element.className = `data-state${kind ? ` ${kind}` : ''}`;
}

function appendTextElement(parent, tag, value, className = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text(value);
    parent.appendChild(node);
    return node;
}

function createDemandFilters(container, groupName, selectedLevels) {
    DEMAND_LEVELS.forEach((level) => {
        const label = document.createElement('label');
        label.className = `demand-option ${groupName}-option`;
        label.title = `${groupName === 'passenger' ? 'Passagier' : 'Cargo'}-Aufkommen ${level}`;
        const input = document.createElement('input');
        input.type = 'checkbox'; input.checked = true; input.value = String(level); input.dataset.demandGroup = groupName;
        const caption = document.createElement('span');
        caption.textContent = String(level);
        label.append(input, caption);
        input.addEventListener('change', () => {
            if (input.checked) selectedLevels.add(level); else selectedLevels.delete(level);
            updateLayerFilters(); schedulePermalink();
        });
        container.appendChild(label);
    });
}

function restoreLevelSet(param, selectedLevels, groupName) {
    if (param === null) return;
    selectedLevels.clear();
    param.split(',').filter((value) => value !== '').map(Number)
        .filter((level) => DEMAND_LEVELS.includes(level)).forEach((level) => selectedLevels.add(level));
    document.querySelectorAll(`input[data-demand-group="${groupName}"]`).forEach((input) => { input.checked = selectedLevels.has(Number(input.value)); });
}

function updateLayerFilters() {
    const { layerFilter, destinationFilter } = demandFilterHelpers;
    if (map.getLayer('passenger-airports')) {
        map.setFilter('passenger-airports', layerFilter('passenger_demand', selectedPassengerLevels));
        map.setLayoutProperty('passenger-airports', 'visibility', selectedPassengerLevels.size ? 'visible' : 'none');
    }
    if (map.getLayer('cargo-airports')) {
        map.setFilter('cargo-airports', layerFilter('cargo_demand', selectedCargoLevels));
        map.setLayoutProperty('cargo-airports', 'visibility', selectedCargoLevels.size ? 'visible' : 'none');
    }
    if (map.getLayer('flightplan-destinations')) map.setFilter(
        'flightplan-destinations', destinationFilter(selectedPassengerLevels, selectedCargoLevels)
    );
    elements.togglePassenger.textContent = selectedPassengerLevels.size === DEMAND_LEVELS.length ? 'alle ausblenden' : 'alle einblenden';
    elements.toggleCargo.textContent = selectedCargoLevels.size === DEMAND_LEVELS.length ? 'alle ausblenden' : 'alle einblenden';
    const passengerCount = airportData.features.filter((feature) => selectedPassengerLevels.has(Number(feature.properties.passenger_demand))).length;
    const cargoCount = airportData.features.filter((feature) => selectedCargoLevels.has(Number(feature.properties.cargo_demand))).length;
    setState(elements.airportCount, `${numberText(passengerCount)} Passagierpunkte · ${numberText(cargoCount)} Cargopunkte sichtbar`);
}

function updateVisibility() {
    if (map.getLayer('flightplan-routes')) map.setLayoutProperty('flightplan-routes', 'visibility', elements.routeToggle.checked ? 'visible' : 'none');
    if (map.getLayer('flightplan-destinations')) map.setLayoutProperty('flightplan-destinations', 'visibility', elements.routeToggle.checked ? 'visible' : 'none');
    if (map.getLayer('flightplan-aircraft')) map.setLayoutProperty('flightplan-aircraft', 'visibility', elements.aircraftToggle.checked ? 'visible' : 'none');
}

function comparisonColor() {
    return ['match', ['get', 'comparison_class'], 'primary_only', '#8b5cf6', 'secondary_only', '#f97316', 'comparison_only', '#f97316', 'shared', '#22c55e', '#8b5cf6'];
}

function addMapData() {
    map.addSource('airports', { type: 'geojson', data: airportData });
    map.addLayer({ id: 'passenger-airports', type: 'circle', source: 'airports', paint: { 'circle-color': '#38bdf8', 'circle-radius': ['interpolate', ['linear'], ['get', 'passenger_demand'], 0, 2, 10, 9], 'circle-stroke-color': '#fff', 'circle-stroke-width': .7, 'circle-opacity': .78 } });
    map.addLayer({ id: 'cargo-airports', type: 'circle', source: 'airports', paint: { 'circle-color': '#f97316', 'circle-radius': ['interpolate', ['linear'], ['get', 'cargo_demand'], 0, 2, 10, 8], 'circle-stroke-color': '#111827', 'circle-stroke-width': .6, 'circle-opacity': .62, 'circle-translate': [5, 5] } });
    map.addSource('flightplan-routes', { type: 'geojson', data: routeData });
    map.addLayer({ id: 'flightplan-routes', type: 'line', source: 'flightplan-routes', paint: { 'line-color': comparisonColor(), 'line-width': ['interpolate', ['linear'], ['coalesce', ['get', 'flight_count'], 1], 1, 1.2, 20, 3.2, 128, 8], 'line-opacity': .76 } });
    map.addSource('flightplan-destinations', { type: 'geojson', data: destinationData });
    map.addLayer({ id: 'flightplan-destinations', type: 'circle', source: 'flightplan-destinations', paint: { 'circle-color': comparisonColor(), 'circle-radius': ['interpolate', ['linear'], ['coalesce', ['get', 'flight_count'], 1], 1, 4, 40, 9], 'circle-stroke-color': '#fff', 'circle-stroke-width': 1.2, 'circle-opacity': .9 } });
    map.addSource('flightplan-aircraft', { type: 'geojson', data: aircraftData });
    map.addLayer({ id: 'flightplan-aircraft', type: 'symbol', source: 'flightplan-aircraft', layout: { 'text-field': '➤', 'text-size': ['interpolate', ['linear'], ['zoom'], 1, 15, 5, 20], 'text-allow-overlap': true, 'text-ignore-placement': true, 'text-rotate': ['-', ['get', 'bearing'], 90], 'text-rotation-alignment': 'map', 'text-pitch-alignment': 'map' }, paint: { 'text-color': '#facc15', 'text-halo-color': '#111827', 'text-halo-width': 1.2 } });
    updateLayerFilters(); updateVisibility(); bindMapInteractions();
}

function updateSource(id, data) {
    const source = map.getSource(id);
    if (source) source.setData(data);
}

function popupNode(properties, type) {
    const root = document.createElement('div');
    if (type === 'route') {
        appendTextElement(root, 'p', `${text(properties.origin_iata, '---')} → ${text(properties.destination_iata, '---')}`, 'popup-title');
        appendTextElement(root, 'p', `${numberText(properties.flight_count)} Flüge · ${text(properties.aircraft_types)}`, 'popup-detail');
    } else if (type === 'aircraft') {
        appendTextElement(root, 'p', properties.flight_number || 'Flug', 'popup-title');
        appendTextElement(root, 'p', `${text(properties.origin_iata)} → ${text(properties.destination_iata)}`, 'popup-detail');
        appendTextElement(root, 'p', `${text(properties.departure_time)}–${text(properties.arrival_time)} · ${text(properties.aircraft_type)}`, 'popup-detail');
    } else {
        appendTextElement(root, 'p', `${text(properties.iata_code || properties.destination_iata, '---')} · ${text(properties.name || properties.destination_name || properties.icao_code)}`, 'popup-title');
        appendTextElement(root, 'p', `Flüge: ${numberText(properties.flight_count || 0)}`, 'popup-detail');
    }
    return root;
}

function bindMapInteractions() {
    const interactiveLayers = ['flightplan-aircraft', 'flightplan-destinations', 'passenger-airports', 'cargo-airports', 'flightplan-routes'];
    map.on('mousemove', (event) => {
        map.getCanvas().style.cursor = map.queryRenderedFeatures(event.point, { layers: interactiveLayers }).length ? 'pointer' : '';
    });
    map.on('click', (event) => {
        const features = map.queryRenderedFeatures(event.point, { layers: interactiveLayers });
        const aircraft = features.find((feature) => feature.layer.id === 'flightplan-aircraft');
        if (aircraft) {
            new mapboxgl.Popup().setLngLat(event.lngLat).setDOMContent(popupNode(aircraft.properties, 'aircraft')).addTo(map);
            return;
        }
        const airport = features.find((feature) => ['flightplan-destinations', 'passenger-airports', 'cargo-airports'].includes(feature.layer.id));
        if (airport) {
            const properties = airport.properties;
            const airportId = properties.airport_id ?? properties.destination_airport_id ?? properties.id;
            if (airportId !== undefined && airportId !== null) openAirport(String(airportId), properties);
            return;
        }
        const route = features.find((feature) => feature.layer.id === 'flightplan-routes');
        if (route) new mapboxgl.Popup().setLngLat(event.lngLat).setDOMContent(popupNode(route.properties, 'route')).addTo(map);
    });
}

function selectedAirlineIds() {
    return [elements.airlineSelect.value, elements.comparisonSelect.value]
        .filter((value) => value && value !== ALL_AIRLINES);
}

function apiParams() {
    const params = new URLSearchParams();
    const ids = selectedAirlineIds();
    if (ids.length) params.set('enterprise_ids', ids.join(','));
    return params;
}

async function fetchJson(path, label, signal) {
    const response = await fetch(path, { signal });
    if (!response.ok) throw new Error(`${label}: HTTP ${response.status}`);
    return response.json();
}

function normalizeGeoJson(data) {
    if (data && data.type === 'FeatureCollection' && Array.isArray(data.features)) return data;
    if (data && Array.isArray(data.features)) return { type: 'FeatureCollection', features: data.features };
    if (Array.isArray(data)) return { type: 'FeatureCollection', features: data };
    return EMPTY_GEOJSON();
}

function normalizeDestinations(data) {
    const normalized = normalizeGeoJson(data?.destinations || data);
    const airports = new Map(airportData.features.map((feature) => [String(feature.properties.airport_id), feature]));
    normalized.features = normalized.features.map((item) => {
        const feature = item?.type === 'Feature' ? item : null;
        const properties = feature?.properties || item;
        const airlineIds = (properties.airlines || []).map((airline) => String(airline.enterprise_id ?? airline.id ?? airline));
        const primary = elements.airlineSelect.value;
        const comparison = elements.comparisonSelect.value;
        const hasPrimary = airlineIds.includes(primary);
        const hasComparison = airlineIds.includes(comparison);
        properties.comparison_class = comparison
            ? (hasPrimary && hasComparison ? 'shared' : hasComparison ? 'secondary_only' : 'primary_only')
            : 'primary_only';
        const airportId = properties.airport_id ?? properties.destination_airport_id ?? properties.id;
        const airport = airports.get(String(airportId));
        properties.airport_id = airportId;
        properties.passenger_demand ??= airport?.properties?.passenger_demand;
        properties.cargo_demand ??= airport?.properties?.cargo_demand;
        if (feature) return { ...feature, properties };
        const coordinates = airport?.geometry?.coordinates || [];
        const longitude = Number(item.longitude ?? item.lng ?? item.destination_longitude ?? coordinates[0]);
        const latitude = Number(item.latitude ?? item.lat ?? item.destination_latitude ?? coordinates[1]);
        return {
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [longitude, latitude] },
            properties
        };
    }).filter((feature) => feature.geometry?.coordinates?.every(Number.isFinite));
    return normalized;
}

async function settledApi(path, label, signal) {
    try { return { data: await fetchJson(path, label, signal) }; }
    catch (error) { if (error.name === 'AbortError') throw error; return { error }; }
}

function clearDashboard() {
    dataRequestId += 1;
    loadDashboard.controller?.abort();
    routeData = EMPTY_GEOJSON();
    destinationData = EMPTY_GEOJSON();
    aircraftData = EMPTY_GEOJSON();
    updateSource('flightplan-routes', routeData);
    updateSource('flightplan-destinations', destinationData);
    updateSource('flightplan-aircraft', aircraftData);
    clearKpis();
    setState(elements.routeState, 'Airline auswählen, um den Flugplan anzuzeigen.', 'empty');
    setState(elements.destinationState, 'Airline auswählen, um Ziele anzuzeigen.', 'empty');
    setState(elements.aircraftState, 'Airline auswählen, um Flugzeuge anzuzeigen.', 'empty');
    setState(elements.kpiState, 'Airline auswählen, um Kennzahlen anzuzeigen.', 'empty');
    updateVisibility();
    if (selectedAirportId) openAirport(selectedAirportId, selectedAirportProperties);
}

async function loadDashboard() {
    if (!elements.airlineSelect.value) {
        clearDashboard();
        return;
    }
    const requestId = ++dataRequestId;
    if (selectedAirportId) {
        detailRequestId += 1;
        elements.drawerContent.replaceChildren();
        setState(elements.drawerState, 'Flugplandetails werden aktualisiert …');
    }

    const controller = new AbortController();
    loadDashboard.controller?.abort(); loadDashboard.controller = controller;
    const query = apiParams().toString();
    elements.airlineSelect.disabled = true; elements.comparisonSelect.disabled = true;
    setState(elements.routeState, 'Flugplan wird geladen …');
    setState(elements.destinationState, 'Ziele werden geladen …');
    setState(elements.aircraftState, 'Flugzeuge werden geladen …');
    setState(elements.kpiState, 'Kennzahlen werden geladen …');
    try {
        const [routesResult, destinationsResult, aircraftResult, kpisResult] = await Promise.all([
            settledApi(`/api/flightplan/routes?${query}`, 'Routen', controller.signal),
            settledApi(`/api/flightplan/destinations?${query}`, 'Ziele', controller.signal),
            settledApi(`/api/flightplan/aircraft?${query}`, 'Flugzeuge', controller.signal),
            settledApi(`/api/flightplan/kpis?${query}`, 'Kennzahlen', controller.signal)
        ]);
        if (requestId !== dataRequestId) return;
        if (routesResult.error) {
            routeData = EMPTY_GEOJSON(); updateSource('flightplan-routes', routeData);
            setState(elements.routeState, 'Routen konnten nicht geladen werden.', 'error');
        }
        else {
            routeData = normalizeGeoJson(routesResult.data); updateSource('flightplan-routes', routeData);
            setState(elements.routeState, routeData.features.length ? `${numberText(routeData.features.length)} Routen geladen` : 'Keine Routen für diese Auswahl.', routeData.features.length ? '' : 'empty');
        }
        if (destinationsResult.error) {
            destinationData = EMPTY_GEOJSON(); updateSource('flightplan-destinations', destinationData);
            setState(elements.destinationState, 'Ziele konnten nicht geladen werden.', 'error');
        }
        else {
            destinationData = normalizeDestinations(destinationsResult.data); updateSource('flightplan-destinations', destinationData);
            setState(elements.destinationState, destinationData.features.length ? `${numberText(destinationData.features.length)} Ziele im Flugplan` : 'Keine Ziele für diese Auswahl.', destinationData.features.length ? '' : 'empty');
        }
        if (aircraftResult.error) {
            aircraftData = EMPTY_GEOJSON(); updateSource('flightplan-aircraft', aircraftData);
            setState(elements.aircraftState, 'Flugzeuge konnten nicht geladen werden.', 'error');
        }
        else {
            aircraftData = normalizeGeoJson(aircraftResult.data); updateSource('flightplan-aircraft', aircraftData);
            setState(elements.aircraftState, aircraftData.features.length ? `${numberText(aircraftData.features.length)} Flugzeuge aktiv` : 'Keine aktiven Flugzeuge.', aircraftData.features.length ? '' : 'empty');
        }
        if (kpisResult.error) { clearKpis(); setState(elements.kpiState, 'Kennzahlen konnten nicht geladen werden.', 'error'); }
        else renderKpis(kpisResult.data, routesResult.error ? EMPTY_GEOJSON() : routeData);
        updateVisibility();
        if (selectedAirportId) openAirport(selectedAirportId, selectedAirportProperties);
    } catch (error) {
        if (error.name !== 'AbortError') console.error(error);
    } finally {
        if (requestId === dataRequestId) {
            elements.airlineSelect.disabled = elements.airlineSelect.options.length <= 1;
            syncComparisonOptions();
        }
    }
}

function kpiCards(data, response) {
    const source = data.kpis || data.summary || data;
    const candidates = [
        ['Flüge', source.flight_count ?? source.flights ?? source.total_flights],
        ['Routen', source.route_count ?? source.routes ?? source.total_routes],
        ['Ziele', source.destination_count ?? source.destinations ?? source.total_destinations],
        ['Flugzeuge', source.aircraft_count ?? source.aircraft ?? source.active_aircraft],
        ['Distanz', source.total_distance_km ?? source.distance_km],
        ['Airlines', source.airline_count ?? response?.metadata?.airlines?.length ?? selectedAirlineIds().length]
    ];
    return candidates.filter(([, value]) => value !== undefined && value !== null).slice(0, 6);
}

function clearKpis() { elements.kpiGrid.replaceChildren(); elements.longestRoutes.replaceChildren(); }

function routeDistanceKm(feature) {
    const coordinates = feature.geometry?.coordinates || [];
    let distance = 0;
    for (let index = 1; index < coordinates.length; index += 1) {
        const [lng1, lat1] = coordinates[index - 1].map((value) => value * Math.PI / 180);
        const [lng2, lat2] = coordinates[index].map((value) => value * Math.PI / 180);
        const a = Math.sin((lat2 - lat1) / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin((lng2 - lng1) / 2) ** 2;
        distance += 12742 * Math.asin(Math.sqrt(a));
    }
    return Math.round(distance);
}

function renderKpis(response, routes) {
    clearKpis();
    const data = response?.data || response || {};
    const cards = kpiCards(data || {}, response);
    cards.forEach(([label, value]) => {
        const card = document.createElement('div'); card.className = 'kpi-card';
        appendTextElement(card, 'strong', label === 'Distanz' ? `${numberText(value)} km` : numberText(value));
        appendTextElement(card, 'span', label); elements.kpiGrid.appendChild(card);
    });
    const longest = data.longest_routes || data.longestRoutes || data.routes_by_distance || routes.features.map((feature) => ({
        ...feature.properties, distance_km: routeDistanceKm(feature)
    })).sort((first, second) => second.distance_km - first.distance_km);
    longest.slice(0, 5).forEach((route) => {
        const item = document.createElement('li');
        const origin = route.origin_iata || route.origin || route.from;
        const destination = route.destination_iata || route.destination || route.to;
        item.textContent = `${text(origin)} ${route.bidirectional ? '↔' : '→'} ${text(destination)} · ${numberText(route.distance_km ?? route.distance)} km`;
        elements.longestRoutes.appendChild(item);
    });
    const hasData = cards.length || longest.length;
    setState(elements.kpiState, hasData ? 'Kennzahlen für die aktuelle Auswahl' : 'Keine Kennzahlen für diese Auswahl.', hasData ? '' : 'empty');
}

function populateAirlines(airlines) {
    const primaryFromUrl = initialParams.get('airlines')?.split(',')[0] || '';
    const comparisonFromUrl = initialParams.get('airlines')?.split(',')[1] || '';
    elements.airlineSelect.replaceChildren(new Option('Airline auswählen', ''));
    const allAirlines = new Option('Alle Airlines', ALL_AIRLINES);
    allAirlines.className = 'all-airlines-option';
    elements.airlineSelect.add(allAirlines);
    elements.comparisonSelect.replaceChildren(new Option('Kein Vergleich', ''));
    airlines.forEach((airline) => {
        const id = text(airline.enterprise_id || airline.id, '');
        const label = `${text(airline.enterprise_name || airline.name, `Airline ${id}`)} (${numberText(airline.flight_count || 0)} Flüge)`;
        elements.airlineSelect.add(new Option(label, id)); elements.comparisonSelect.add(new Option(label, id));
    });
    if ([...elements.airlineSelect.options].some((option) => option.value === primaryFromUrl)) elements.airlineSelect.value = primaryFromUrl;
    if ([...elements.comparisonSelect.options].some((option) => option.value === comparisonFromUrl)) elements.comparisonSelect.value = comparisonFromUrl;
    elements.airlineSelect.disabled = false; elements.comparisonSelect.disabled = false; syncComparisonOptions();
}

function syncComparisonOptions() {
    const canCompare = Boolean(elements.airlineSelect.value && elements.airlineSelect.value !== ALL_AIRLINES);
    elements.airlineSelect.classList.toggle('all-airlines-selected', elements.airlineSelect.value === ALL_AIRLINES);
    [...elements.comparisonSelect.options].forEach((option) => { option.disabled = !canCompare || Boolean(option.value && option.value === elements.airlineSelect.value); });
    if (!canCompare || (elements.comparisonSelect.value && elements.comparisonSelect.value === elements.airlineSelect.value)) elements.comparisonSelect.value = '';
    elements.comparisonSelect.disabled = !canCompare;
    elements.comparisonLegend.hidden = !elements.comparisonSelect.value;
}

function detailRows(data, properties) {
    const airport = data.airport || data.details || data;
    return [
        ['IATA / ICAO', `${text(airport.iata_code || properties.iata_code || properties.destination_iata)} / ${text(airport.icao_code || properties.icao_code)}`],
        ['Land', airport.country || properties.country], ['Größe', airport.airport_size || properties.airport_size],
        ['Passagiere', airport.passenger_demand ?? properties.passenger_demand], ['Cargo', airport.cargo_demand ?? properties.cargo_demand]
    ];
}

function scheduledTime(value, fallback) {
    if (!value) return text(fallback);
    return new Intl.DateTimeFormat('de-DE', {
        weekday: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short'
    }).format(new Date(value));
}

function renderFlightSection(title, timeLabel, flights, instantField, fallbackField) {
    appendTextElement(elements.drawerContent, 'h3', title);
    if (!flights.length) {
        appendTextElement(elements.drawerContent, 'p', 'Keine anstehenden Flüge.', 'data-state empty');
        return;
    }
    const list = document.createElement('div'); list.className = 'flight-list';
    flights.forEach((flight) => {
        const item = document.createElement('div'); item.className = 'flight-item';
        appendTextElement(item, 'strong', `${text(flight.flight_number, 'Flug')} · ${text(flight.origin_iata || flight.origin)} → ${text(flight.destination_iata || flight.destination)}`);
        const eventTime = flight[instantField] || flight[fallbackField];
        appendTextElement(item, 'span', `${timeLabel}: ${scheduledTime(eventTime)} · ${text(flight.aircraft_type)}`);
        list.appendChild(item);
    });
    elements.drawerContent.appendChild(list);
}

async function openAirport(airportId, properties = {}) {
    const requestId = ++detailRequestId;
    selectedAirportId = airportId;
    selectedAirportProperties = properties;
    elements.drawer.classList.add('open'); elements.drawer.setAttribute('aria-hidden', 'false');
    elements.drawerTitle.textContent = `${text(properties.iata_code || properties.destination_iata, 'Flughafen')} · Details`;
    elements.drawerContent.replaceChildren(); setState(elements.drawerState, 'Flugplandetails werden geladen …');
    schedulePermalink(airportId);
    if (!elements.airlineSelect.value) {
        const grid = document.createElement('div'); grid.className = 'detail-grid';
        detailRows({ airport: properties }, properties).forEach(([label, value]) => {
            const item = document.createElement('div'); item.className = 'detail-item'; appendTextElement(item, 'span', label); appendTextElement(item, 'strong', value); grid.appendChild(item);
        });
        elements.drawerContent.appendChild(grid);
        setState(elements.drawerState, 'Airline auswählen, um die nächsten Flüge anzuzeigen.', 'empty');
        return;
    }
    try {
        const query = apiParams().toString();
        const data = await fetchJson(`/api/airports/${encodeURIComponent(airportId)}/flightplan?${query}`, 'Flughafendetails');
        if (requestId !== detailRequestId) return;
        const airport = data.airport || data.details || data;
        elements.drawerTitle.textContent = `${text(airport.iata_code || properties.iata_code || properties.destination_iata, 'Flughafen')} · ${text(airport.name || airport.airport_name || properties.destination_name, 'Details')}`;
        const grid = document.createElement('div'); grid.className = 'detail-grid';
        detailRows(data, properties).forEach(([label, value]) => {
            const item = document.createElement('div'); item.className = 'detail-item'; appendTextElement(item, 'span', label); appendTextElement(item, 'strong', value); grid.appendChild(item);
        });
        elements.drawerContent.appendChild(grid);
        const departing = data.departing || data.departures || [];
        const arriving = data.arriving || data.arrivals || [];
        const flights = [...departing, ...arriving];
        renderFlightSection('Nächste Abflüge', 'Abflug', departing, 'next_departure_at', 'departure_time');
        renderFlightSection('Nächste Ankünfte', 'Ankunft', arriving, 'next_arrival_at', 'arrival_time');
        const connections = data.strongest_connections || [];
        if (connections.length) {
            appendTextElement(elements.drawerContent, 'h3', 'Stärkste Verbindungen');
            const list = document.createElement('div'); list.className = 'flight-list';
            connections.forEach((connection) => {
                const item = document.createElement('div'); item.className = 'flight-item';
                appendTextElement(item, 'strong', `${text(connection.iata, '---')} · ${text(connection.name)}`);
                appendTextElement(item, 'span', `${numberText(connection.flight_count)} Flüge`);
                list.appendChild(item);
            });
            elements.drawerContent.appendChild(list);
        }
        setState(elements.drawerState, flights.length ? `${numberText(flights.length)} Flüge` : 'Keine Flüge für diese Auswahl.', flights.length ? '' : 'empty');
    } catch (error) {
        if (requestId === detailRequestId) setState(elements.drawerState, 'Flughafendetails konnten nicht geladen werden.', 'error');
    }
}

function closeDrawer() {
    detailRequestId += 1; selectedAirportId = ''; selectedAirportProperties = {};
    elements.drawer.classList.remove('open'); elements.drawer.setAttribute('aria-hidden', 'true'); schedulePermalink('');
}

function currentAirportFromUrl() { return initialParams.get('airport') || ''; }

function schedulePermalink(selectedAirport) {
    clearTimeout(permalinkTimer);
    permalinkTimer = setTimeout(() => {
        const params = new URLSearchParams();
        const ids = selectedAirlineIds();
        if (elements.airlineSelect.value === ALL_AIRLINES) params.set('airlines', ALL_AIRLINES);
        else if (ids.length) params.set('airlines', ids.join(','));
        params.set('passenger', [...selectedPassengerLevels].sort((a, b) => a - b).join(','));
        params.set('cargo', [...selectedCargoLevels].sort((a, b) => a - b).join(','));
        params.set('routes', elements.routeToggle.checked ? '1' : '0'); params.set('aircraft', elements.aircraftToggle.checked ? '1' : '0');
        const center = map.getCenter(); params.set('center', `${center.lng.toFixed(4)},${center.lat.toFixed(4)}`);
        params.set('zoom', map.getZoom().toFixed(2)); params.set('bearing', map.getBearing().toFixed(1)); params.set('pitch', map.getPitch().toFixed(1));
        const airport = selectedAirport === undefined ? selectedAirportId : selectedAirport;
        if (airport) params.set('airport', airport);
        history.replaceState(null, '', `${location.pathname}?${params}`);
    }, 250);
}

function toggleGroup(groupName) {
    const inputs = [...document.querySelectorAll(`input[data-demand-group="${groupName}"]`)];
    const selected = groupName === 'passenger' ? selectedPassengerLevels : selectedCargoLevels;
    const nextLevels = demandFilterHelpers.toggledLevels(DEMAND_LEVELS, selected);
    selected.clear(); nextLevels.forEach((level) => selected.add(level));
    inputs.forEach((input) => { input.checked = selected.has(Number(input.value)); });
    updateLayerFilters(); schedulePermalink();
}

async function initialize() {
    createDemandFilters(elements.passengerFilters, 'passenger', selectedPassengerLevels);
    createDemandFilters(elements.cargoFilters, 'cargo', selectedCargoLevels);
    restoreLevelSet(initialParams.get('passenger'), selectedPassengerLevels, 'passenger');
    restoreLevelSet(initialParams.get('cargo'), selectedCargoLevels, 'cargo');
    elements.routeToggle.checked = initialParams.get('routes') !== '0'; elements.aircraftToggle.checked = initialParams.get('aircraft') !== '0';
    const [airportsResult, airlinesResult] = await Promise.all([
        settledApi('/api/airports', 'Flughäfen'), settledApi('/api/flightplan/airlines', 'Airlines')
    ]);
    if (airportsResult.error) setState(elements.airportCount, 'Flughäfen konnten nicht geladen werden.', 'error');
    else airportData = normalizeGeoJson(airportsResult.data);
    if (airlinesResult.error) {
        elements.airlineSelect.replaceChildren(new Option('Airlines nicht verfügbar', '')); elements.comparisonSelect.replaceChildren(new Option('Kein Vergleich', ''));
        setState(elements.routeState, 'Airlines konnten nicht geladen werden.', 'error');
    } else populateAirlines(Array.isArray(airlinesResult.data) ? airlinesResult.data : airlinesResult.data.airlines || []);
    addMapData(); mapReady = true;
    if (elements.airlineSelect.value) await loadDashboard(); else clearDashboard();
    const airport = currentAirportFromUrl(); if (airport) openAirport(airport);
}

elements.togglePassenger.addEventListener('click', () => toggleGroup('passenger'));
elements.toggleCargo.addEventListener('click', () => toggleGroup('cargo'));
elements.routeToggle.addEventListener('change', () => { updateVisibility(); schedulePermalink(); });
elements.aircraftToggle.addEventListener('change', () => { updateVisibility(); schedulePermalink(); });
elements.airlineSelect.addEventListener('change', () => { syncComparisonOptions(); loadDashboard(); schedulePermalink(); });
elements.comparisonSelect.addEventListener('change', () => { syncComparisonOptions(); loadDashboard(); schedulePermalink(); });
elements.drawerClose.addEventListener('click', closeDrawer);
map.on('moveend', () => { if (mapReady) schedulePermalink(); });
map.on('load', initialize);

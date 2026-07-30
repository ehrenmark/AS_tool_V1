mapboxgl.accessToken = 'pk.eyJ1IjoibWFya21vZGUiLCJhIjoiY21zODBnbTkyMDF0ZDM1c2c1dTN6OXEzNSJ9.MMJEVkIXt_h0HSnjAlN8uA';

const DEMAND_LEVELS = Array.from({ length: 11 }, (_, index) => index);
const selectedPassengerLevels = new Set(DEMAND_LEVELS);
const selectedCargoLevels = new Set(DEMAND_LEVELS);
const passengerFilters = document.getElementById('passengerFilters');
const cargoFilters = document.getElementById('cargoFilters');
const airportCount = document.getElementById('airportCount');
const routeCount = document.getElementById('routeCount');
const routeToggle = document.getElementById('routeToggle');
const aircraftToggle = document.getElementById('aircraftToggle');
let airportData = { type: 'FeatureCollection', features: [] };
let routeData = { type: 'FeatureCollection', features: [] };
let aircraftData = { type: 'FeatureCollection', features: [] };

const map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/dark-v11',
    center: [10, 30],
    zoom: 1.6,
    projection: 'globe'
});

map.addControl(new mapboxgl.NavigationControl(), 'top-right');
map.addControl(new mapboxgl.FullscreenControl(), 'top-right');

map.on('style.load', () => {
    map.setFog({
        color: 'rgb(186, 210, 235)',
        'high-color': 'rgb(36, 92, 223)',
        'horizon-blend': 0.02,
        'space-color': 'rgb(11, 11, 25)',
        'star-intensity': 0.6
    });
});

function createDemandFilters(container, groupName, selectedLevels) {
    DEMAND_LEVELS.forEach((level) => {
        const label = document.createElement('label');
        label.className = `demand-option ${groupName}-option`;
        label.title = `${groupName === 'passenger' ? 'Passagier' : 'Cargo'}-Aufkommen ${level}`;

        label.innerHTML = `
            <input type="checkbox" data-demand-group="${groupName}" value="${level}" checked>
            <span class="option-content">
                <span class="cross" aria-hidden="true"></span>
                <span class="option-label">${level}</span>
            </span>
        `;

        label.querySelector('input').addEventListener('change', (event) => {
            const demandLevel = Number(event.target.value);
            if (event.target.checked) {
                selectedLevels.add(demandLevel);
            } else {
                selectedLevels.delete(demandLevel);
            }
            updateLayerFilters();
        });

        container.appendChild(label);
    });
}

function buildLayerFilter(propertyName, selectedLevels) {
    if (selectedLevels.size === 0) {
        return false;
    }

    return ['in', ['get', propertyName], ['literal', [...selectedLevels]]];
}

function updateLayerFilters() {
    if (map.getLayer('passenger-airports')) {
        map.setFilter('passenger-airports', buildLayerFilter('passenger_demand', selectedPassengerLevels));
    }
    if (map.getLayer('cargo-airports')) {
        map.setFilter('cargo-airports', buildLayerFilter('cargo_demand', selectedCargoLevels));
    }

    const passengerCount = airportData.features.filter((feature) => selectedPassengerLevels.has(feature.properties.passenger_demand)).length;
    const cargoCount = airportData.features.filter((feature) => selectedCargoLevels.has(feature.properties.cargo_demand)).length;
    airportCount.textContent = `${passengerCount} Passagierpunkte, ${cargoCount} Cargo-Punkte sichtbar`;
}

function updateRouteVisibility() {
    if (map.getLayer('flightplan-routes')) {
        map.setLayoutProperty('flightplan-routes', 'visibility', routeToggle.checked ? 'visible' : 'none');
    }
    routeCount.textContent = routeToggle.checked
        ? `${routeData.features.length} Flugplan-Routen sichtbar`
        : `${routeData.features.length} Flugplan-Routen ausgeblendet`;
}

function updateAircraftVisibility() {
    if (map.getLayer('flightplan-aircraft')) {
        map.setLayoutProperty('flightplan-aircraft', 'visibility', aircraftToggle.checked ? 'visible' : 'none');
    }
}

function toggleGroup(groupName) {
    const inputs = [...document.querySelectorAll(`input[data-demand-group="${groupName}"]`)];
    const selectedLevels = groupName === 'passenger' ? selectedPassengerLevels : selectedCargoLevels;
    const shouldSelectAll = inputs.some((input) => !input.checked);

    selectedLevels.clear();
    inputs.forEach((input) => {
        input.checked = shouldSelectAll;
        if (shouldSelectAll) {
            selectedLevels.add(Number(input.value));
        }
    });

    updateLayerFilters();
}

async function loadGeoJson(url, label) {
    const response = await fetch(url);
    if (!response.ok) {
        throw new Error(`${label} konnten nicht geladen werden: ${response.status}`);
    }
    return response.json();
}

function addRouteLayer(routes) {
    routeData = routes;

    map.addSource('flightplan-routes', {
        type: 'geojson',
        data: routeData
    });

    map.addLayer({
        id: 'flightplan-routes',
        type: 'line',
        source: 'flightplan-routes',
        paint: {
            'line-color': '#a855f7',
            'line-width': [
                'interpolate',
                ['linear'],
                ['get', 'flight_count'],
                1, 1,
                20, 3.2,
                64, 6,
                128, 9
            ],
            'line-opacity': 0.68
        }
    });

    updateRouteVisibility();
}

function addAircraftLayer(aircraft) {
    aircraftData = aircraft;

    map.addSource('flightplan-aircraft', {
        type: 'geojson',
        data: aircraftData
    });

    map.addLayer({
        id: 'flightplan-aircraft',
        type: 'symbol',
        source: 'flightplan-aircraft',
        layout: {
            'text-field': '\u2708',
            'text-size': ['interpolate', ['linear'], ['zoom'], 1, 13, 5, 18],
            'text-allow-overlap': true,
            'text-ignore-placement': true,
            'text-rotate': ['get', 'bearing'],
            'text-rotation-alignment': 'map'
        },
        paint: {
            'text-color': '#facc15',
            'text-halo-color': '#111827',
            'text-halo-width': 1.2
        }
    });

    updateAircraftVisibility();
}

function addAirportLayers(airports) {
    airportData = airports;

    map.addSource('airports', {
        type: 'geojson',
        data: airportData
    });

    map.addLayer({
        id: 'passenger-airports',
        type: 'circle',
        source: 'airports',
        paint: {
            'circle-color': '#38bdf8',
            'circle-radius': ['interpolate', ['linear'], ['get', 'passenger_demand'], 0, 2, 10, 9],
            'circle-stroke-color': '#ffffff',
            'circle-stroke-width': 0.7,
            'circle-opacity': 0.78
        }
    });

    map.addLayer({
        id: 'cargo-airports',
        type: 'circle',
        source: 'airports',
        paint: {
            'circle-color': '#f97316',
            'circle-radius': ['interpolate', ['linear'], ['get', 'cargo_demand'], 0, 2, 10, 8],
            'circle-stroke-color': '#111827',
            'circle-stroke-width': 0.6,
            'circle-opacity': 0.62,
            'circle-translate': [5, 5]
        }
    });

    updateLayerFilters();
}

function airportPopupHtml(properties) {
    return `
        <p class="popup-title">${properties.iata_code || '---'} / ${properties.icao_code || '---'}</p>
        <p class="popup-detail">ID: ${properties.airport_id}</p>
        <p class="popup-detail">Land: ${properties.country || '-'}</p>
        <p class="popup-detail">Passagiere: ${properties.passenger_demand}</p>
        <p class="popup-detail">Cargo: ${properties.cargo_demand}</p>
    `;
}

function routePopupHtml(properties) {
    return `
        <p class="popup-title">${properties.origin_iata} -> ${properties.destination_iata}</p>
        <p class="popup-detail">${properties.origin_name || properties.origin_iata} nach ${properties.destination_name || properties.destination_iata}</p>
        <p class="popup-detail">Fluege: ${properties.flight_count}</p>
        <p class="popup-detail">Typen: ${properties.aircraft_types || '-'}</p>
        <p class="popup-detail">Airline: ${properties.enterprise_name || '-'}</p>
    `;
}

function aircraftPopupHtml(properties) {
    return `
        <p class="popup-title">${properties.flight_number || 'Flugzeug'}</p>
        <p class="popup-detail">${properties.origin_iata} -> ${properties.destination_iata}</p>
        <p class="popup-detail">Typ: ${properties.aircraft_type || '-'}</p>
        <p class="popup-detail">Abflug: ${properties.departure_time || '-'}</p>
        <p class="popup-detail">Ankunft: ${properties.arrival_time || '-'}</p>
        <p class="popup-detail">Fortschritt: ${Math.round((properties.progress || 0) * 100)}%</p>
    `;
}

function addPopup(layerId, htmlBuilder, useClickLngLat = false) {
    map.on('click', layerId, (event) => {
        const feature = event.features[0];
        new mapboxgl.Popup()
            .setLngLat(useClickLngLat ? event.lngLat : feature.geometry.coordinates.slice())
            .setHTML(htmlBuilder(feature.properties))
            .addTo(map);
    });

    map.on('mouseenter', layerId, () => {
        map.getCanvas().style.cursor = 'pointer';
    });
    map.on('mouseleave', layerId, () => {
        map.getCanvas().style.cursor = '';
    });
}

createDemandFilters(passengerFilters, 'passenger', selectedPassengerLevels);
createDemandFilters(cargoFilters, 'cargo', selectedCargoLevels);

document.querySelectorAll('[data-toggle-group]').forEach((button) => {
    button.addEventListener('click', () => toggleGroup(button.dataset.toggleGroup));
});

routeToggle.addEventListener('change', updateRouteVisibility);
aircraftToggle.addEventListener('change', updateAircraftVisibility);

map.on('load', async () => {
    try {
        const [airports, routes, aircraft] = await Promise.all([
            loadGeoJson('/api/airports', 'Flughaefen'),
            loadGeoJson('/api/flightplan/routes', 'Flugplan-Routen'),
            loadGeoJson('/api/flightplan/aircraft', 'Flugzeuge')
        ]);
        addRouteLayer(routes);
        addAircraftLayer(aircraft);
        addAirportLayers(airports);
        addPopup('flightplan-routes', routePopupHtml, true);
        addPopup('flightplan-aircraft', aircraftPopupHtml);
        addPopup('passenger-airports', airportPopupHtml);
        addPopup('cargo-airports', airportPopupHtml);
    } catch (error) {
        airportCount.textContent = 'Daten konnten nicht geladen werden.';
        routeCount.textContent = 'Daten konnten nicht geladen werden.';
        console.error(error);
    }
});
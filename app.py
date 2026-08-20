import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request


ROUTE_SEGMENT_DEGREES = 2


def angular_distance(origin, destination):
    origin_lng, origin_lat = map(math.radians, origin)
    destination_lng, destination_lat = map(math.radians, destination)
    cosine = (
        math.sin(origin_lat) * math.sin(destination_lat)
        + math.cos(origin_lat) * math.cos(destination_lat)
        * math.cos(destination_lng - origin_lng)
    )
    return math.acos(max(-1, min(1, cosine)))


def interpolate_route_position(origin, destination, progress):
    progress = max(0, min(1, progress))
    origin_lng, origin_lat = map(math.radians, origin)
    destination_lng, destination_lat = map(math.radians, destination)
    distance = angular_distance(origin, destination)
    if distance == 0:
        return list(origin)

    delta_lng = destination_lng - origin_lng
    bearing = math.atan2(
        math.sin(delta_lng) * math.cos(destination_lat),
        math.cos(origin_lat) * math.sin(destination_lat)
        - math.sin(origin_lat) * math.cos(destination_lat) * math.cos(delta_lng),
    )
    travelled = distance * progress
    latitude = math.asin(
        math.sin(origin_lat) * math.cos(travelled)
        + math.cos(origin_lat) * math.sin(travelled) * math.cos(bearing)
    )
    longitude = origin_lng + math.atan2(
        math.sin(bearing) * math.sin(travelled) * math.cos(origin_lat),
        math.cos(travelled) - math.sin(origin_lat) * math.sin(latitude),
    )
    return [math.degrees(longitude), math.degrees(latitude)]


def shortest_line_coordinates(origin_longitude, origin_latitude, destination_longitude, destination_latitude):
    origin = [origin_longitude, origin_latitude]
    destination = [destination_longitude, destination_latitude]
    segment_count = max(
        1,
        math.ceil(math.degrees(angular_distance(origin, destination)) / ROUTE_SEGMENT_DEGREES),
    )
    coordinates = [
        interpolate_route_position(origin, destination, step / segment_count)
        for step in range(segment_count + 1)
    ]
    coordinates[0] = origin

    for index in range(1, len(coordinates)):
        while coordinates[index][0] - coordinates[index - 1][0] > 180:
            coordinates[index][0] -= 360
        while coordinates[index][0] - coordinates[index - 1][0] < -180:
            coordinates[index][0] += 360
    coordinates[-1][1] = destination_latitude
    return coordinates


def interpolate_route_geometry_position(origin, destination, progress):
    coordinates = shortest_line_coordinates(*origin, *destination)
    scaled_progress = max(0, min(1, progress)) * (len(coordinates) - 1)
    segment_index = min(math.floor(scaled_progress), len(coordinates) - 2)
    segment_progress = scaled_progress - segment_index
    start, end = coordinates[segment_index], coordinates[segment_index + 1]
    return [
        start[axis] + (end[axis] - start[axis]) * segment_progress
        for axis in range(2)
    ]


def route_bearing(start, end):
    start_lng, start_lat = map(math.radians, start)
    end_lng, end_lat = map(math.radians, end)
    delta_lng = end_lng - start_lng
    x = math.sin(delta_lng) * math.cos(end_lat)
    y = (
        math.cos(start_lat) * math.sin(end_lat)
        - math.sin(start_lat) * math.cos(end_lat) * math.cos(delta_lng)
    )
    return (math.degrees(math.atan2(x, y)) + 360) % 360

app = Flask(__name__)
DATABASE_PATH = Path(__file__).resolve().parent / 'data' / 'airlinesim.sqlite3'


def empty_feature_collection():
    return {'type': 'FeatureCollection', 'features': []}


def connect_database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def parse_analysis_filters():
    singular = request.args.getlist('enterprise_id')
    plural = request.args.getlist('enterprise_ids')
    if singular and plural:
        raise ValueError('enterprise_id und enterprise_ids dürfen nicht gemeinsam verwendet werden')
    if len(singular) > 1:
        raise ValueError('enterprise_id darf nur einmal angegeben werden')

    raw_ids = singular if singular else [part for value in plural for part in value.split(',')]
    enterprise_ids = []
    for value in raw_ids:
        value = value.strip()
        if not value or not value.isdigit() or int(value) <= 0:
            field = 'enterprise_id' if singular else 'enterprise_ids'
            raise ValueError(f'{field} muss positive ganze Zahlen enthalten')
        enterprise_id = int(value)
        if enterprise_id > 9223372036854775807:
            raise ValueError('enterprise IDs sind zu groß')
        if enterprise_id not in enterprise_ids:
            enterprise_ids.append(enterprise_id)
    if len(enterprise_ids) > 2:
        raise ValueError('enterprise_ids darf höchstens zwei eindeutige IDs enthalten')

    weekday_value = request.args.get('weekday')
    if len(request.args.getlist('weekday')) > 1:
        raise ValueError('weekday darf nur einmal angegeben werden')
    weekday = None
    if weekday_value is not None:
        if not weekday_value.isdigit() or not 1 <= int(weekday_value) <= 7:
            raise ValueError('weekday muss eine ganze Zahl zwischen 1 und 7 sein')
        weekday = int(weekday_value)

    time_value = request.args.get('time')
    if len(request.args.getlist('time')) > 1:
        raise ValueError('time darf nur einmal angegeben werden')
    minute = None
    if time_value is not None:
        if (len(time_value) != 5 or time_value[2] != ':'
                or not time_value[:2].isdigit() or not time_value[3:].isdigit()
                or int(time_value[:2]) > 23 or int(time_value[3:]) > 59):
            raise ValueError('time muss dem Format HH:MM entsprechen')
        minute = int(time_value[:2]) * 60 + int(time_value[3:])
    return {'enterprise_ids': enterprise_ids, 'weekday': weekday,
            'time': time_value, 'minute': minute}


def selected_enterprise_id():
    filters = parse_analysis_filters()
    return filters['enterprise_ids'][0] if filters['enterprise_ids'] else None


def error_response(error):
    return jsonify({'error': str(error)}), 400


def table_columns(connection, table):
    if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return set()
    return {row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def ensure_analysis_indexes(connection):
    """Create only indexes supported by the currently installed schema."""
    columns = table_columns(connection, 'flightplan_flights')
    definitions = [
        ('idx_flightplan_enterprise', ('enterprise_id',)),
        ('idx_flightplan_route', ('origin_airport_id', 'destination_airport_id')),
        ('idx_flightplan_schedule', ('frequency', 'departure_time', 'arrival_time')),
    ]
    created = []
    for name, index_columns in definitions:
        if set(index_columns) <= columns:
            candidate = name
            suffix = 1
            while True:
                existing = connection.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                    (candidate,),
                ).fetchone()
                if existing is None:
                    break
                existing_columns = tuple(
                    row['name'] for row in connection.execute(f'PRAGMA index_info("{candidate}")')
                )
                if existing['tbl_name'] == 'flightplan_flights' and existing_columns == index_columns:
                    break
                suffix += 1
                candidate = f'{name}_analysis{suffix}'
            quoted = ', '.join(f'"{column}"' for column in index_columns)
            connection.execute(
                f'CREATE INDEX IF NOT EXISTS "{candidate}" ON flightplan_flights ({quoted})'
            )
            created.append(candidate)
    return created


@app.cli.command('init-analysis-schema')
def init_analysis_schema():
    """Create optional analytics indexes in a controlled maintenance step."""
    if not DATABASE_PATH.exists():
        return
    connection = connect_database()
    try:
        ensure_analysis_indexes(connection)
        connection.commit()
    finally:
        connection.close()


def flight_table_exists(connection):
    return bool(table_columns(connection, 'flightplan_flights'))


def optional_expression(columns, name, alias='f'):
    return f'{alias}."{name}"' if name in columns else f'NULL AS "{name}"'


def load_flights(connection, filters, require_coordinates=False):
    columns = table_columns(connection, 'flightplan_flights')
    airport_columns = table_columns(connection, 'airports')
    required = {'enterprise_id', 'origin_airport_id', 'destination_airport_id'}
    if not required <= columns or 'airport_id' not in airport_columns:
        return []
    if require_coordinates and not {'latitude', 'longitude'} <= airport_columns:
        return []
    names = ['enterprise_name', 'flight_number', 'frequency', 'departure_time',
             'arrival_time', 'aircraft_type', 'origin_iata', 'origin_name',
             'destination_iata', 'destination_name']
    select_optional = ', '.join(optional_expression(columns, name) for name in names)
    where, parameters = ['f.enterprise_id IS NOT NULL'], []
    ids = filters['enterprise_ids']
    if ids:
        where.append('f.enterprise_id IN (' + ','.join('?' for _ in ids) + ')')
        parameters.extend(ids)
    origin_latitude = 'origin.latitude' if 'latitude' in airport_columns else 'NULL'
    origin_longitude = 'origin.longitude' if 'longitude' in airport_columns else 'NULL'
    destination_latitude = 'destination.latitude' if 'latitude' in airport_columns else 'NULL'
    destination_longitude = 'destination.longitude' if 'longitude' in airport_columns else 'NULL'
    sql = f'''SELECT f.enterprise_id, f.origin_airport_id, f.destination_airport_id,
                     {select_optional},
                     {origin_latitude} AS origin_latitude, {origin_longitude} AS origin_longitude,
                     {destination_latitude} AS destination_latitude,
                     {destination_longitude} AS destination_longitude
              FROM flightplan_flights f
              LEFT JOIN airports origin ON origin.airport_id=f.origin_airport_id
              LEFT JOIN airports destination ON destination.airport_id=f.destination_airport_id'''
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    rows = [dict(row) for row in connection.execute(sql, parameters)]
    if filters['weekday'] is not None:
        day = str(filters['weekday'])
        rows = [row for row in rows if day in (row.get('frequency') or '')]
    if require_coordinates:
        rows = [row for row in rows if all(row.get(key) is not None for key in
                ('origin_latitude', 'origin_longitude', 'destination_latitude', 'destination_longitude'))]
    return rows


def airline_summary(rows):
    airlines = {}
    for row in rows:
        enterprise_id = row['enterprise_id']
        item = airlines.setdefault(enterprise_id, {
            'enterprise_id': enterprise_id,
            'enterprise_name': row.get('enterprise_name') or f'Airline {enterprise_id}',
            'flight_count': 0,
        })
        item['flight_count'] += 1
    return [airlines[key] for key in sorted(airlines)]


def filter_metadata(filters, rows):
    return {
        'enterprise_ids': filters['enterprise_ids'],
        'weekday': filters['weekday'],
        'time': filters['time'],
        'flight_count': len(rows),
        'airlines': airline_summary(rows),
    }


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/airports')
def airports():
    if not DATABASE_PATH.exists():
        return jsonify(empty_feature_collection())

    connection = connect_database()
    try:
        rows = connection.execute(
            """
            SELECT
                airport_id,
                iata_code,
                icao_code,
                country,
                airport_size,
                passenger_demand,
                cargo_demand,
                latitude,
                longitude
            FROM airports
            WHERE latitude IS NOT NULL
              AND longitude IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()

    features = [
        {
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [row['longitude'], row['latitude']],
            },
            'properties': {
                'airport_id': row['airport_id'],
                'iata_code': row['iata_code'],
                'icao_code': row['icao_code'],
                'country': row['country'],
                'airport_size': row['airport_size'],
                'passenger_demand': row['passenger_demand'],
                'cargo_demand': row['cargo_demand'],
            },
        }
        for row in rows
    ]

    return jsonify({'type': 'FeatureCollection', 'features': features})


@app.route('/api/flightplan/airlines')
def flightplan_airlines():
    if not DATABASE_PATH.exists():
        return jsonify([])

    connection = connect_database()
    try:
        columns = table_columns(connection, 'flightplan_flights')
        if 'enterprise_id' not in columns:
            return jsonify([])
        enterprise_name = (
            "COALESCE(NULLIF(enterprise_name, ''), 'Airline ' || enterprise_id)"
            if 'enterprise_name' in columns else "'Airline ' || enterprise_id"
        )
        rows = connection.execute(
            f"""
            SELECT
                enterprise_id,
                {enterprise_name} AS enterprise_name,
                COUNT(*) AS flight_count
            FROM flightplan_flights
            WHERE enterprise_id IS NOT NULL
            GROUP BY enterprise_id
            ORDER BY enterprise_name COLLATE NOCASE, enterprise_id
            """
        ).fetchall()
    finally:
        connection.close()

    return jsonify([dict(row) for row in rows])


@app.route('/api/flightplan/routes')
def flightplan_routes():
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    result = empty_feature_collection()
    result['metadata'] = filter_metadata(filters, [])
    result['metadata']['route_count'] = 0
    if not DATABASE_PATH.exists():
        return jsonify(result)
    connection = connect_database()
    try:
        rows = load_flights(connection, filters, require_coordinates=True)
    finally:
        connection.close()
    grouped = {}
    for row in rows:
        key = (row['origin_airport_id'], row['destination_airport_id'])
        group = grouped.setdefault(key, {'rows': [], 'airlines': set(), 'types': set()})
        group['rows'].append(row)
        group['airlines'].add(row['enterprise_id'])
        if row.get('aircraft_type'):
            group['types'].add(row['aircraft_type'])
    features = []
    selected = filters['enterprise_ids']
    for group in grouped.values():
        row = group['rows'][0]
        airline_ids = sorted(group['airlines'])
        comparison_class = None
        if len(selected) == 2:
            comparison_class = ('shared' if set(selected) <= group['airlines'] else
                                'primary_only' if selected[0] in group['airlines'] else 'secondary_only')
        properties = {
            'airline_count': len(airline_ids),
            'origin_airport_id': row['origin_airport_id'], 'origin_iata': row.get('origin_iata'),
            'destination_airport_id': row['destination_airport_id'],
            'destination_iata': row.get('destination_iata'),
            'flight_count': len(group['rows']),
            'aircraft_types': ','.join(sorted(group['types'])) or None,
            'comparison_class': comparison_class,
        }
        if selected:
            properties['enterprise_ids'] = airline_ids
            properties['airlines'] = airline_summary(group['rows'])
            properties['origin_name'] = row.get('origin_name')
            properties['destination_name'] = row.get('destination_name')
        if len(airline_ids) == 1:
            properties['enterprise_id'] = airline_ids[0]
            properties['enterprise_name'] = (row.get('enterprise_name') or f'Airline {airline_ids[0]}')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': shortest_line_coordinates(
                row['origin_longitude'], row['origin_latitude'],
                row['destination_longitude'], row['destination_latitude'])},
            'properties': properties,
        })
    result['features'] = features
    result['metadata'] = filter_metadata(filters, rows)
    result['metadata']['route_count'] = len(features)
    result['metadata']['geometry_detail'] = 'great_circle'
    return jsonify(result)

def parse_time_to_minutes(value):
    if not value or len(value) != 5 or value[2] != ':':
        return None
    try:
        hours, minutes = map(int, value.split(':'))
    except ValueError:
        return None
    return hours * 60 + minutes if 0 <= hours < 24 and 0 <= minutes < 60 else None


def is_flight_active(departure_minute, arrival_minute, now_minute):
    duration = arrival_minute - departure_minute
    if duration <= 0:
        duration += 24 * 60

    elapsed = now_minute - departure_minute
    if elapsed < 0:
        elapsed += 24 * 60

    if elapsed > duration:
        return None
    return elapsed / duration


def next_flight_occurrence(row, now, event):
    departure_minute = parse_time_to_minutes(row.get('departure_time'))
    arrival_minute = parse_time_to_minutes(row.get('arrival_time'))
    if departure_minute is None or arrival_minute is None:
        return None
    flight_days = {int(day) for day in (row.get('frequency') or '') if day in '1234567'}
    if not flight_days:
        return None
    arrival_day_offset = 1 if arrival_minute <= departure_minute else 0
    start = now - timedelta(days=1) if event == 'arrival' else now
    for day_offset in range(9):
        departure_date = (start + timedelta(days=day_offset)).date()
        if departure_date.isoweekday() not in flight_days:
            continue
        minute = arrival_minute if event == 'arrival' else departure_minute
        event_date = departure_date + timedelta(days=arrival_day_offset if event == 'arrival' else 0)
        occurrence = datetime.combine(event_date, datetime.min.time(), timezone.utc) + timedelta(minutes=minute)
        if occurrence >= now:
            return occurrence
    return None


@app.route('/api/flightplan/aircraft')
def flightplan_aircraft():
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    result = empty_feature_collection()
    result['metadata'] = filter_metadata(filters, [])
    result['metadata']['aircraft_count'] = 0
    now = datetime.now(timezone.utc)
    result['metadata']['instant'] = filters['time'] or now.strftime('%H:%M')
    if not DATABASE_PATH.exists():
        return jsonify(result)
    weekday = filters['weekday'] or now.isoweekday()
    now_minute = filters['minute'] if filters['minute'] is not None else (
        now.hour * 60 + now.minute + now.second / 60 + now.microsecond / 60_000_000
    )
    yesterday = 7 if weekday == 1 else weekday - 1
    connection = connect_database()
    try:
        query_filters = dict(filters)
        query_filters['weekday'] = None
        rows = load_flights(connection, query_filters, require_coordinates=True)
    finally:
        connection.close()
    aircraft_features = []
    active_rows = []
    for row in rows:
        departure_minute = parse_time_to_minutes(row['departure_time'])
        arrival_minute = parse_time_to_minutes(row['arrival_time'])
        if departure_minute is None or arrival_minute is None:
            continue

        flight_days = row.get('frequency') or ''
        arrival_is_next_day = arrival_minute <= departure_minute
        today_active = str(weekday) in flight_days and now_minute >= departure_minute
        if not arrival_is_next_day:
            today_active = str(weekday) in flight_days
        previous_overnight = arrival_is_next_day and str(yesterday) in flight_days and now_minute <= arrival_minute
        if not today_active and not previous_overnight:
            continue
        progress = is_flight_active(departure_minute, arrival_minute, now_minute)
        if previous_overnight:
            duration = arrival_minute + 1440 - departure_minute
            progress = (now_minute + 1440 - departure_minute) / duration
        if progress is None:
            continue
        active_rows.append(row)

        origin = [row['origin_longitude'], row['origin_latitude']]
        destination = [row['destination_longitude'], row['destination_latitude']]
        duration_minutes = arrival_minute - departure_minute
        if duration_minutes <= 0:
            duration_minutes += 1440
        departure_at = now - timedelta(minutes=progress * duration_minutes)
        arrival_at = departure_at + timedelta(minutes=duration_minutes)
        position = interpolate_route_geometry_position(origin, destination, progress)
        bearing_position = interpolate_route_geometry_position(
            origin,
            destination,
            min(1, progress + 0.001),
        )
        if progress == 1:
            bearing_position = position
            position_for_bearing = interpolate_route_geometry_position(origin, destination, progress - 0.001)
        else:
            position_for_bearing = position
        aircraft_features.append(
            {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': position,
                },
                'properties': {
                    'enterprise_id': row['enterprise_id'],
                    'enterprise_name': row['enterprise_name'],
                    'flight_number': row['flight_number'],
                    'aircraft_type': row['aircraft_type'],
                    'origin_iata': row['origin_iata'],
                    'origin_name': row['origin_name'],
                    'destination_iata': row['destination_iata'],
                    'destination_name': row['destination_name'],
                    'departure_time': row['departure_time'],
                    'arrival_time': row['arrival_time'],
                    'progress': round(progress, 3),
                    'bearing': route_bearing(position_for_bearing, bearing_position),
                    'departure_at': departure_at.isoformat(),
                    'arrival_at': arrival_at.isoformat(),
                    'origin_coordinates': origin,
                    'destination_coordinates': destination,
                },
            }
        )

    result['features'] = aircraft_features
    result['metadata'] = filter_metadata(filters, active_rows)
    result['metadata']['aircraft_count'] = len(aircraft_features)
    result['metadata']['instant'] = filters['time'] or now.strftime('%H:%M')
    return jsonify(result)


def analysis_rows(filters):
    if not DATABASE_PATH.exists():
        return []
    connection = connect_database()
    try:
        return load_flights(connection, filters)
    finally:
        connection.close()


@app.route('/api/analysis/kpis')
@app.route('/api/flightplan/kpis')
def analysis_kpis():
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    rows = analysis_rows(filters)
    route_sets = {}
    undirected_route_rows = {}
    for row in rows:
        route_key = (row['origin_airport_id'], row['destination_airport_id'])
        route_sets.setdefault(route_key, set()).add(row['enterprise_id'])
        undirected_route_rows.setdefault(tuple(sorted(route_key)), row)
    aircraft_type_counts = {}
    for row in rows:
        aircraft_type = row.get('aircraft_type')
        if aircraft_type:
            aircraft_type_counts[aircraft_type] = aircraft_type_counts.get(aircraft_type, 0) + 1
    longest_routes = []
    for row in undirected_route_rows.values():
        coordinates = (row.get('origin_longitude'), row.get('origin_latitude'),
                       row.get('destination_longitude'), row.get('destination_latitude'))
        if any(value is None for value in coordinates):
            continue
        distance_km = round(6371.0088 * angular_distance(
            [coordinates[0], coordinates[1]], [coordinates[2], coordinates[3]]
        ))
        longest_routes.append({
            'origin_airport_id': row['origin_airport_id'], 'origin_iata': row.get('origin_iata'),
            'destination_airport_id': row['destination_airport_id'],
            'destination_iata': row.get('destination_iata'),
            'distance_km': distance_km,
            'bidirectional': True,
        })
    longest_routes.sort(key=lambda item: (-item['distance_km'], item['origin_airport_id'],
                                          item['destination_airport_id']))
    data = {
        'flights': len(rows), 'routes': len(route_sets),
        'destinations': len({row['destination_airport_id'] for row in rows}),
        'airports': len({value for row in rows for value in
                         (row['origin_airport_id'], row['destination_airport_id'])}),
        'aircraft_types': len(aircraft_type_counts),
        'aircraft_type_counts': dict(sorted(aircraft_type_counts.items())),
        'longest_routes': longest_routes[:5],
    }
    if len(filters['enterprise_ids']) == 2:
        first, second = filters['enterprise_ids']
        data['comparison'] = {
            'primary_only': sum(1 for ids in route_sets.values() if first in ids and second not in ids),
            'secondary_only': sum(1 for ids in route_sets.values() if second in ids and first not in ids),
            'shared': sum(1 for ids in route_sets.values() if first in ids and second in ids),
        }
    return jsonify({'data': data, 'metadata': filter_metadata(filters, rows)})


@app.route('/api/analysis/destinations')
@app.route('/api/flightplan/destinations')
def analysis_destinations():
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    rows = analysis_rows(filters)
    destinations = {}
    for row in rows:
        item = destinations.setdefault(row['destination_airport_id'], {
            'airport_id': row['destination_airport_id'], 'iata': row.get('destination_iata'),
            'name': row.get('destination_name'), 'flight_count': 0, 'routes': set(), 'rows': []})
        item['flight_count'] += 1
        item['routes'].add(row['origin_airport_id'])
        item['rows'].append(row)
    data = []
    for item in destinations.values():
        item['route_count'] = len(item.pop('routes'))
        item['airlines'] = airline_summary(item.pop('rows'))
        data.append(item)
    data.sort(key=lambda item: (-item['flight_count'], item['airport_id']))
    return jsonify({'destinations': data, 'metadata': filter_metadata(filters, rows)})


@app.route('/api/analysis/airports/<int:airport_id>/flightplan')
@app.route('/api/airports/<int:airport_id>/flightplan')
def analysis_airport_flightplan(airport_id):
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    rows = analysis_rows(filters)
    all_departing = [row for row in rows if row['origin_airport_id'] == airport_id]
    all_arriving = [row for row in rows if row['destination_airport_id'] == airport_id]
    airport = None
    if DATABASE_PATH.exists():
        connection = connect_database()
        try:
            columns = table_columns(connection, 'airports')
            if 'airport_id' in columns:
                selected = sorted(columns)
                quoted = ','.join(f'"{name}"' for name in selected)
                row = connection.execute(
                    f'SELECT {quoted} FROM airports WHERE airport_id=?', (airport_id,)
                ).fetchone()
                airport = dict(row) if row else None
        finally:
            connection.close()
    if airport is None:
        return jsonify({'error': 'airport not found'}), 404
    airport_rows = all_departing + all_arriving
    destination_groups = {'outbound': {}, 'inbound': {}}
    connection_groups = {}
    for direction, items in (('outbound', all_departing), ('inbound', all_arriving)):
        for item in items:
            if direction == 'outbound':
                other_id, other_iata, other_name = (item['destination_airport_id'],
                    item.get('destination_iata'), item.get('destination_name'))
            else:
                other_id, other_iata, other_name = (item['origin_airport_id'],
                    item.get('origin_iata'), item.get('origin_name'))
            group = destination_groups[direction].setdefault(other_id, {
                'airport_id': other_id, 'iata': other_iata, 'name': other_name,
                'flight_count': 0,
            })
            group['flight_count'] += 1
            connection = connection_groups.setdefault(other_id, {
                'airport_id': other_id, 'iata': other_iata, 'name': other_name,
                'flight_count': 0,
            })
            connection['flight_count'] += 1
    destinations = {
        direction: sorted(groups.values(), key=lambda item: (-item['flight_count'], item['airport_id']))
        for direction, groups in destination_groups.items()
    }
    strongest_connections = sorted(
        connection_groups.values(), key=lambda item: (-item['flight_count'], item['airport_id'])
    )[:10]
    serialize = lambda row: {key: row.get(key) for key in ('enterprise_id', 'enterprise_name', 'flight_number',
        'frequency', 'departure_time', 'arrival_time', 'aircraft_type', 'origin_airport_id', 'origin_iata',
        'origin_name', 'destination_airport_id', 'destination_iata', 'destination_name')}
    now = datetime.now(timezone.utc)
    def next_rows(items, event):
        occurrences = [(next_flight_occurrence(row, now, event), row) for row in items]
        scheduled = [(instant, row) for instant, row in occurrences if instant is not None]
        scheduled.sort(key=lambda item: (
            item[0], item[1].get('flight_number') or '', item[1].get('enterprise_id') or 0
        ))
        result = []
        for instant, row in scheduled[:10]:
            item = serialize(row)
            item[f'next_{event}_at'] = instant.isoformat()
            result.append(item)
        return result
    departing = next_rows(all_departing, 'departure')
    arriving = next_rows(all_arriving, 'arrival')
    return jsonify({'airport': airport, 'departing': departing,
                    'arriving': arriving,
                    'airlines': airline_summary(airport_rows), 'destinations': destinations,
                    'strongest_connections': strongest_connections,
                    'metadata': filter_metadata(filters, airport_rows)})


@app.route('/api/analysis/import-status')
@app.route('/api/flightplan/import-status')
def analysis_import_status():
    try:
        filters = parse_analysis_filters()
    except ValueError as error:
        return error_response(error)
    response = {'available': False, 'latest': None, 'metadata': filter_metadata(filters, [])}
    if not DATABASE_PATH.exists():
        return jsonify(response)
    connection = connect_database()
    try:
        columns = table_columns(connection, 'import_runs')
        if not columns:
            return jsonify(response)
        response['available'] = True
        order = 'id DESC' if 'id' in columns else ('started_at DESC' if 'started_at' in columns else None)
        where = " WHERE import_type='flightplans'" if 'import_type' in columns else ''
        ordering = f' ORDER BY {order}' if order else ''
        row = connection.execute(f'SELECT * FROM import_runs{where}{ordering} LIMIT 1').fetchone()
        response['latest'] = dict(row) if row else None
        if response['latest'] and 'completed_at' in response['latest']:
            response['latest']['finished_at'] = response['latest']['completed_at']
    finally:
        connection.close()
    return jsonify(response)


if __name__ == '__main__':
    app.run(debug=True)
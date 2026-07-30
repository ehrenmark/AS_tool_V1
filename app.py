import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template


def shortest_line_coordinates(origin_longitude, origin_latitude, destination_longitude, destination_latitude):
    adjusted_destination_longitude = destination_longitude
    delta = destination_longitude - origin_longitude
    if delta > 180:
        adjusted_destination_longitude -= 360
    elif delta < -180:
        adjusted_destination_longitude += 360

    return [
        [origin_longitude, origin_latitude],
        [adjusted_destination_longitude, destination_latitude],
    ]


def interpolate_route_position(coordinates, progress):
    start, end = coordinates
    return [
        start[0] + (end[0] - start[0]) * progress,
        start[1] + (end[1] - start[1]) * progress,
    ]


def route_bearing(coordinates):
    start, end = coordinates
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


@app.route('/api/flightplan/routes')
def flightplan_routes():
    if not DATABASE_PATH.exists():
        return jsonify(empty_feature_collection())

    connection = connect_database()
    try:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'flightplan_flights'
            """
        ).fetchone()
        if not table_exists:
            return jsonify(empty_feature_collection())

        rows = connection.execute(
            """
            SELECT
                f.enterprise_id,
                f.enterprise_name,
                f.origin_airport_id,
                f.origin_iata,
                f.origin_name,
                origin.latitude AS origin_latitude,
                origin.longitude AS origin_longitude,
                f.destination_airport_id,
                f.destination_iata,
                f.destination_name,
                destination.latitude AS destination_latitude,
                destination.longitude AS destination_longitude,
                COUNT(*) AS flight_count,
                GROUP_CONCAT(DISTINCT f.aircraft_type) AS aircraft_types
            FROM flightplan_flights f
            JOIN airports origin
              ON origin.airport_id = f.origin_airport_id
            JOIN airports destination
              ON destination.airport_id = f.destination_airport_id
            WHERE origin.latitude IS NOT NULL
              AND origin.longitude IS NOT NULL
              AND destination.latitude IS NOT NULL
              AND destination.longitude IS NOT NULL
            GROUP BY
                f.enterprise_id,
                f.origin_airport_id,
                f.destination_airport_id
            """
        ).fetchall()
    finally:
        connection.close()

    features = [
        {
            'type': 'Feature',
            'geometry': {
                'type': 'LineString',
                'coordinates': shortest_line_coordinates(
                    row['origin_longitude'],
                    row['origin_latitude'],
                    row['destination_longitude'],
                    row['destination_latitude'],
                ),
            },
            'properties': {
                'enterprise_id': row['enterprise_id'],
                'enterprise_name': row['enterprise_name'],
                'origin_airport_id': row['origin_airport_id'],
                'origin_iata': row['origin_iata'],
                'origin_name': row['origin_name'],
                'destination_airport_id': row['destination_airport_id'],
                'destination_iata': row['destination_iata'],
                'destination_name': row['destination_name'],
                'flight_count': row['flight_count'],
                'aircraft_types': row['aircraft_types'],
            },
        }
        for row in rows
    ]

    return jsonify({'type': 'FeatureCollection', 'features': features})

def parse_time_to_minutes(value):
    if not value or ':' not in value:
        return None
    hours, minutes = value.split(':', 1)
    return int(hours) * 60 + int(minutes)


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


@app.route('/api/flightplan/aircraft')
def flightplan_aircraft():
    if not DATABASE_PATH.exists():
        return jsonify(empty_feature_collection())

    now = datetime.now(timezone.utc)
    now_minute = now.hour * 60 + now.minute
    today = str(now.isoweekday())
    yesterday = str(7 if now.isoweekday() == 1 else now.isoweekday() - 1)

    connection = connect_database()
    try:
        table_exists = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'flightplan_flights'
            """
        ).fetchone()
        if not table_exists:
            return jsonify(empty_feature_collection())

        rows = connection.execute(
            """
            SELECT
                f.enterprise_id,
                f.enterprise_name,
                f.flight_number,
                f.frequency,
                f.departure_time,
                f.arrival_time,
                f.aircraft_type,
                f.origin_airport_id,
                f.origin_iata,
                f.origin_name,
                origin.latitude AS origin_latitude,
                origin.longitude AS origin_longitude,
                f.destination_airport_id,
                f.destination_iata,
                f.destination_name,
                destination.latitude AS destination_latitude,
                destination.longitude AS destination_longitude
            FROM flightplan_flights f
            JOIN airports origin
              ON origin.airport_id = f.origin_airport_id
            JOIN airports destination
              ON destination.airport_id = f.destination_airport_id
            WHERE origin.latitude IS NOT NULL
              AND origin.longitude IS NOT NULL
              AND destination.latitude IS NOT NULL
              AND destination.longitude IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()

    aircraft_features = []
    for row in rows:
        departure_minute = parse_time_to_minutes(row['departure_time'])
        arrival_minute = parse_time_to_minutes(row['arrival_time'])
        if departure_minute is None or arrival_minute is None:
            continue

        flight_days = row['frequency'] or ''
        arrival_is_next_day = arrival_minute <= departure_minute
        if today not in flight_days and not (arrival_is_next_day and yesterday in flight_days):
            continue

        progress = is_flight_active(departure_minute, arrival_minute, now_minute)
        if progress is None:
            continue

        coordinates = shortest_line_coordinates(
            row['origin_longitude'],
            row['origin_latitude'],
            row['destination_longitude'],
            row['destination_latitude'],
        )
        aircraft_features.append(
            {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': interpolate_route_position(coordinates, progress),
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
                    'bearing': route_bearing(coordinates),
                },
            }
        )

    return jsonify({'type': 'FeatureCollection', 'features': aircraft_features})

if __name__ == '__main__':
    app.run(debug=True)
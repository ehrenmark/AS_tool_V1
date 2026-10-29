import re
from fractions import Fraction


WEEK_MINUTES = 7 * 24 * 60
WEEKDAYS = ('Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So')
MAX_ROUTES = 20


def integer_field(value, label, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,5}', value.strip()):
        raise ValueError(f'{label}: Bitte eine ganze Zahl zwischen {minimum} und {maximum} eingeben.')
    number = int(value.strip())
    if not minimum <= number <= maximum:
        raise ValueError(f'{label}: Der Wert muss zwischen {minimum} und {maximum} liegen.')
    return number


def airport_code(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]{3}', value.strip()):
        raise ValueError(f'{label}: Bitte einen dreistelligen IATA-Code eingeben.')
    return value.strip().upper()


def week_time(minute):
    day, minute_of_day = divmod(minute, 1440)
    suffix = ' (Folgewoche)' if day >= 7 else ''
    return f'{WEEKDAYS[day % 7]}{suffix} {minute_of_day // 60:02d}:{minute_of_day % 60:02d}'


def generate_weekly_plan(base, start_day, start_time, ground_minutes, routes):
    base = airport_code(base, 'Basis')
    day = integer_field(start_day, 'Starttag', 0, 6)
    if not isinstance(start_time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', start_time):
        raise ValueError('Startzeit: Bitte eine UTC-Zeit im Format HH:MM eingeben.')
    hour, minute = map(int, start_time.split(':'))
    start = day * 1440 + hour * 60 + minute
    ground = integer_field(ground_minutes, 'Mindestbodenzeit', 1, 720)
    if not isinstance(routes, list) or not 1 <= len(routes) <= MAX_ROUTES:
        raise ValueError(f'Bitte zwischen 1 und {MAX_ROUTES} Strecken eingeben.')

    parsed = []
    destinations = set()
    for index, route in enumerate(routes, 1):
        label = f'Strecke {index}'
        if not isinstance(route, dict):
            raise ValueError(f'{label}: Ungültige Streckeneingabe.')
        destination = airport_code(route.get('destination'), f'{label}, Ziel')
        if destination == base:
            raise ValueError(f'{label}: Ziel und Basis müssen unterschiedlich sein.')
        if destination in destinations:
            raise ValueError(f'{label}: {destination} ist bereits als Ziel vorhanden.')
        destinations.add(destination)
        parsed.append({
            'destination': destination,
            'outbound': integer_field(route.get('outbound'), f'{label}, Hinflugzeit', 1, 2880),
            'inbound': integer_field(route.get('inbound'), f'{label}, Rückflugzeit', 1, 2880),
            'frequency': integer_field(route.get('frequency'), f'{label}, Umläufe pro Woche', 1, 28),
        })

    # Interleave destinations by their requested weekly frequencies.
    requests = sorted(
        (Fraction(occurrence, route['frequency']), index)
        for index, route in enumerate(parsed)
        for occurrence in range(route['frequency'])
    )
    required = sum((route['outbound'] + route['inbound'] + 2 * ground) * route['frequency']
                   for route in parsed)
    available = WEEK_MINUTES - start
    slack = max(0, available - required)
    flights = []
    idle = []
    unplanned = [0] * len(parsed)
    cursor = start
    for position, (_, route_index) in enumerate(requests):
        route = parsed[route_index]
        duration = route['outbound'] + route['inbound'] + 2 * ground
        if cursor + duration > WEEK_MINUTES:
            unplanned[route_index] += 1
            continue
        # Share spare capacity between rotations, retaining room for all requested flights.
        departure = cursor + (position * slack // len(requests)
                              - (position - 1) * slack // len(requests) if position else 0)
        if departure > cursor:
            idle.append({'start': cursor, 'end': departure, 'minutes': departure - cursor})
        for origin, destination, duration in (
            (base, route['destination'], route['outbound']),
            (route['destination'], base, route['inbound']),
        ):
            arrival = departure + duration
            flights.append({
                'number': len(flights) + 1,
                'origin': origin,
                'destination': destination,
                'departure': departure,
                'arrival': arrival,
                'ready': arrival + ground,
                'duration': duration,
                'departure_label': week_time(departure),
                'arrival_label': week_time(arrival),
                'ready_label': week_time(arrival + ground),
            })
            departure = arrival + ground
        cursor = departure
    if cursor < WEEK_MINUTES:
        idle.append({'start': cursor, 'end': WEEK_MINUTES, 'minutes': WEEK_MINUTES - cursor})
    for gap in idle:
        gap['start_label'] = week_time(gap['start'])
        gap['end_label'] = week_time(gap['end'])

    return {
        'base': base,
        'complete': not any(unplanned),
        'flights': flights,
        'days': [{'name': name, 'flights': [flight for flight in flights
                                           if flight['departure'] // 1440 == index]}
                 for index, name in enumerate(WEEKDAYS)],
        'idle': idle,
        'unplanned': [{'destination': route['destination'], 'count': count}
                      for route, count in zip(parsed, unplanned) if count],
        'summary': {
            'rotations': len(flights) // 2,
            'requested_rotations': len(requests),
            'flight_minutes': sum(flight['duration'] for flight in flights),
            'ground_minutes': len(flights) * ground,
            'idle_minutes': sum(gap['minutes'] for gap in idle),
            'available_minutes': available,
            'required_minutes': required,
            'shortfall_minutes': max(0, required - available),
            'ready_label': week_time(cursor) if flights else None,
        },
    }

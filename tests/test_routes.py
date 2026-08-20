import math
import unittest

from app import (interpolate_route_geometry_position, interpolate_route_position,
                 route_bearing, shortest_line_coordinates)


class RouteGeometryTests(unittest.TestCase):
    def test_great_circle_route_curves_toward_the_pole(self):
        coordinates = shortest_line_coordinates(-74.0, 40.7, 2.35, 48.85)

        self.assertGreater(len(coordinates), 2)
        self.assertGreater(max(point[1] for point in coordinates), 50)
        self.assertEqual(coordinates[0], [-74.0, 40.7])
        self.assertAlmostEqual(coordinates[-1][0], 2.35)
        self.assertAlmostEqual(coordinates[-1][1], 48.85)

    def test_route_uses_short_dateline_crossing(self):
        coordinates = shortest_line_coordinates(170, 10, -170, 10)

        longitude_steps = [
            abs(end[0] - start[0])
            for start, end in zip(coordinates, coordinates[1:])
        ]
        self.assertLess(max(longitude_steps), 5)
        self.assertAlmostEqual(coordinates[-1][0], 190)

    def test_aircraft_position_lies_on_generated_great_circle(self):
        origin = [-74.0, 40.7]
        destination = [2.35, 48.85]
        route = shortest_line_coordinates(*origin, *destination)
        midpoint_index = len(route) // 2
        progress = midpoint_index / (len(route) - 1)
        midpoint = interpolate_route_position(origin, destination, progress)
        route_midpoint = route[midpoint_index]

        self.assertAlmostEqual(midpoint[0], route_midpoint[0])
        self.assertAlmostEqual(midpoint[1], route_midpoint[1])

    def test_bearing_points_in_flight_direction(self):
        bearing = route_bearing([0, 0], [1, 0])

        self.assertTrue(math.isclose(bearing, 90, abs_tol=0.01))

    def test_aircraft_interpolation_is_exactly_on_rendered_segments(self):
        for origin, destination in (([-74, 40.7], [2.35, 48.85]),
                                    ([170, 10], [-170, 10])):
            route = shortest_line_coordinates(*origin, *destination)
            for progress in (.013, .127, .375, .731, .999):
                position = interpolate_route_geometry_position(origin, destination, progress)
                scaled = progress * (len(route) - 1)
                index = min(math.floor(scaled), len(route) - 2)
                fraction = scaled - index
                expected = [route[index][axis] +
                            (route[index + 1][axis] - route[index][axis]) * fraction
                            for axis in range(2)]
                self.assertAlmostEqual(position[0], expected[0])
                self.assertAlmostEqual(position[1], expected[1])


if __name__ == '__main__':
    unittest.main()

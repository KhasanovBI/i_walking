import math

from geojson import LineString, Point
from geomet import wkt
from geopy.distance import great_circle
from rest_framework import views
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api.serializers.map.route import ConcreteRouteSerializer, POIRouteSerializer
from utils.double_gis.geometry import get_center_of_points, get_normal_vector
from utils.double_gis.service import DoubleGisService


class AbstractRouteView(views.APIView):
    SPEED = 3 * 1000 / 60  # метры в минуту
    permission_classes = (AllowAny,)
    api = DoubleGisService().get_api()
    AVERAGE_STRIDE_LENGTH = 0.6  # в метрах
    METADATA_ROUTE_POINTS_COUNT = 4

    def points_to_query(self, points):
        str_points = ['{} {}'.format(*point['coordinates']) for point in points]
        return ','.join(str_points)

    def estimate_walking_time(self, point1, point2):
        # Время в минутах
        return great_circle(point1['coordinates'], point2['coordinates']).meters / self.SPEED

    def serialize_linestring(self, linestring):
        result = [
            {
                'longitude': position[0],
                'latitude': position[1]
            } for position in linestring['coordinates']
            ]
        return result

    def get_points_for_round_route(self, start_point, end_point):
        center_point_coordinates = get_center_of_points(start_point, end_point)
        normal_vector = get_normal_vector(start_point, end_point)
        second_point = Point((center_point_coordinates[0] + normal_vector[0],
                              center_point_coordinates[1] + normal_vector[1]))
        fourth_point = Point((center_point_coordinates[0] - normal_vector[0],
                              center_point_coordinates[1] - normal_vector[1]))
        return start_point, second_point, end_point, fourth_point, start_point

    def build_route(self, points, alternative=0):
        query_points = self.points_to_query(points)
        response = self.api.transport.calculate_directions(
            waypoints=query_points,
            edge_filter='pedestrian',
            alternative=alternative,
        )
        if response['meta']['code'] != 200:
            raise ValidationError(response)
        legs = response['result']['items'][0]['legs']
        linestrings = []
        for leg in legs:
            for step in leg['steps']:
                for edge in step['edges']:
                    linestrings.append(wkt.loads(edge['geometry']['selection']))
        # Первое и последнее ребро - это отметки нулевой длины
        final_linestring_positions = []
        for linestring in linestrings[:-1]:
            final_linestring_positions.extend(linestring['coordinates'][1:])
        return LineString(tuple(final_linestring_positions))

    def _get_route_points_metadata(self, route_points):
        route_points_count = len(route_points)

        if route_points_count <= self.METADATA_ROUTE_POINTS_COUNT:
            metadata_route_points = route_points
        else:
            metadata_route_points = [
                route_points[i * (route_points_count // self.METADATA_ROUTE_POINTS_COUNT)]
                for i in range(1, self.METADATA_ROUTE_POINTS_COUNT)
                ]
        metadata = []
        for point in metadata_route_points:
            response = self.api.geo.search(
                point='{},{}'.format(*point['coordinates']),
                fields='items.geometry.selection',
                radius=100,
                type='attraction,poi',
                page_size=1
            )
            if response['meta']['code'] == 200:
                item = response['result']['items'][0]
                raw_point = wkt.loads(item['geometry']['selection'])
                position = raw_point['coordinates']
                item['point'] = {
                    'longitude': position[0],
                    'latitude': position[1]
                }
                metadata.append(item)
            else:
                pass
        return metadata

    def prepare_route_response(self, walking_time, route_points, end_point_metadata=None,
                               alternatives_count=0):
        return Response(
            {
                'walking_time': round(walking_time, 2),
                'route': self.serialize_linestring(
                    self.build_route(route_points, alternatives_count)
                ),
                'route_points': self._get_route_points_metadata(route_points),
                'steps_count': round(self.SPEED * walking_time / self.AVERAGE_STRIDE_LENGTH),
                'end_point_data': end_point_metadata
            }
        )


class POIRouteView(AbstractRouteView):
    SEARCH_STRING = None
    serializer_class = POIRouteSerializer
    OPTIMAL_WALK_TIME = 30  # в минутах

    def get_search_polygon(self, start_point):
        coordinates = start_point['coordinates']
        point1 = Point((coordinates[0] - 0.029, coordinates[1] + 0.019))
        point2 = Point((coordinates[0] + 0.029, coordinates[1] - 0.019))
        return point1, point2

    def get_search_query_by_type(self, type):
        if type == POIRouteSerializer.BAR:
            return 'Бар'
        elif type == POIRouteSerializer.CULTURE:
            return 'Театр'
        elif type == POIRouteSerializer.FOOD:
            return 'Продукты'
        elif type == POIRouteSerializer.ROMANTIC:
            return 'Кинотеатр'
        elif type == POIRouteSerializer.INVESTIGATE:
            return 'Памятник'

    def search_optimal_organization_point(self, start_point, items):
        optimal_point = None
        optimal_time = None
        metadata = None
        for item in items:
            raw_point = item['point']
            item_point = Point((raw_point['lon'], raw_point['lat']))
            walking_time = math.pi * self.estimate_walking_time(start_point, item_point)
            if (optimal_time is None or math.fabs(optimal_time - self.OPTIMAL_WALK_TIME) >
                math.fabs(walking_time - self.OPTIMAL_WALK_TIME)):
                optimal_time = walking_time
                optimal_point = item_point
                metadata = item
        position = optimal_point['coordinates']
        metadata['point'] = {
            'longitude': position[0],
            'latitude': position[1]
        }
        return optimal_point, metadata

    def search_organization_point(self, start_point, point1, point2, query):
        params = dict(
            point1='{},{}'.format(*point1['coordinates']),
            point2='{},{}'.format(*point2['coordinates']),
            fields='items.point',
            type='attraction,building,poi'
        )
        if query is not None:
            params['q'] = query
        response = self.api.catalog.branch.search(**params)
        if response['meta']['code'] != 200:
            raise ValidationError(response)
        return self.search_optimal_organization_point(start_point, response['result']['items'])

    def search_optimal_geo_point(self, start_point, items):
        optimal_point = None
        optimal_time = None
        metadata = None
        for item in items:
            item_point = wkt.loads(item['geometry']['selection'])
            walking_time = math.pi * self.estimate_walking_time(start_point, item_point)
            if (optimal_time is None or math.fabs(optimal_time - self.OPTIMAL_WALK_TIME) >
                math.fabs(walking_time - self.OPTIMAL_WALK_TIME)):
                optimal_time = walking_time
                optimal_point = item_point
                metadata = item
        position = optimal_point['coordinates']
        metadata['point'] = {
            'longitude': position[0],
            'latitude': position[1]
        }
        return optimal_point, metadata

    def search_geo_point(self, start_point, point1, point2, query):
        params = dict(
            point1='{},{}'.format(*point1['coordinates']),
            point2='{},{}'.format(*point2['coordinates']),
            fields='items.geometry.selection',
            type='attraction,building,poi'
        )
        if query is not None:
            params['q'] = query
        response = self.api.geo.search(**params)
        if response['meta']['code'] == 200:
            return self.search_optimal_geo_point(start_point, response['result']['items'])
        elif query is None:
            raise ValidationError(response)

    def search_destination(self, start_point, type):
        point1, point2 = self.get_search_polygon(start_point)
        query = self.get_search_query_by_type(type)
        result_point_data = self.search_geo_point(start_point, point1, point2, query)
        if result_point_data is None:
            return self.search_organization_point(start_point, point1, point2, query)
        return result_point_data

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_point = serializer.data['point']
        start_point = Point((raw_point['longitude'], raw_point['latitude']))
        end_point, metadata = self.search_destination(start_point, serializer.data['type'])
        walking_time = math.pi * self.estimate_walking_time(start_point, end_point)
        print(
            'Оценочное время прогулки {walking_time} минут.'.format(walking_time=walking_time)
        )
        response = self.prepare_route_response(
            walking_time,
            route_points=self.get_points_for_round_route(start_point, end_point),
            end_point_metadata=metadata
        )
        return response


class ConcreteRouteView(AbstractRouteView):
    serializer_class = ConcreteRouteSerializer
    ALTERNATIVES_COUNT = 5

    def post(self, request, format=None):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_start_point = serializer.data['start_point']
        raw_end_point = serializer.data['end_point']
        start_point = Point((raw_start_point['longitude'], raw_start_point['latitude']))
        end_point = Point((raw_end_point['longitude'], raw_end_point['latitude']))
        walking_time = self.estimate_walking_time(start_point, end_point)
        response = self.prepare_route_response(
            walking_time=walking_time,
            route_points=(start_point, end_point),
            alternatives_count=self.ALTERNATIVES_COUNT,
        )
        return response

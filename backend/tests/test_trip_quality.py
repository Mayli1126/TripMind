from datetime import datetime, timedelta
from unittest import TestCase

from pydantic import ValidationError

from app.models.schemas import (
    Attraction,
    DayPlan,
    Hotel,
    Location,
    Meal,
    TripPlan,
    TripRequest,
    get_app_today,
)
from app.services.trip_quality_service import TripQualityService, trip_quality_service


def make_request(**overrides) -> TripRequest:
    start = datetime.now().date() + timedelta(days=30)
    end = start + timedelta(days=1)
    values = {
        "destination_only": False,  # Explicit legacy cross-city contract.
        "origin_city": "上海",
        "city": "北京",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "travel_days": 2,
        "transportation": "high_speed_train",
        "accommodation": "经济型酒店",
        "travelers": 2,
        "rooms": 1,
        "budget_limit": 1000,
    }
    values.update(overrides)
    return TripRequest(**values)


def make_day(index: int, ticket_price: int) -> DayPlan:
    day_date = datetime.now().date() + timedelta(days=30 + index)
    return DayPlan(
        date=day_date.isoformat(),
        day_index=index,
        description="测试行程",
        transportation="公共交通",
        accommodation="经济型酒店",
        hotel=Hotel(name="测试酒店", estimated_cost=100),
        attractions=[
            Attraction(
                name=f"景点{index}",
                address="测试地址",
                location=Location(longitude=116.4, latitude=39.9),
                visit_duration=120,
                description="测试",
                ticket_price=ticket_price,
            )
        ],
        meals=[
            Meal(type=meal_type, name=meal_type, estimated_cost=10)
            for meal_type in ("breakfast", "lunch", "dinner")
        ],
    )


class TripRequestValidationTests(TestCase):
    def test_rejects_invalid_date(self):
        with self.assertRaises(ValidationError):
            make_request(start_date="not-a-date")

    def test_rejects_inconsistent_day_count(self):
        with self.assertRaises(ValidationError):
            make_request(travel_days=3)

    def test_rejects_rooms_greater_than_travelers(self):
        with self.assertRaises(ValidationError):
            make_request(travelers=1, rooms=2)

    def test_rejects_departure_before_today(self):
        yesterday = get_app_today() - timedelta(days=1)
        with self.assertRaisesRegex(ValidationError, "出发日期不能早于今天"):
            make_request(
                start_date=yesterday.isoformat(),
                end_date=yesterday.isoformat(),
                travel_days=1,
            )

    def test_allows_departure_today(self):
        today = get_app_today().isoformat()
        request = make_request(start_date=today, end_date=today, travel_days=1)
        self.assertEqual(request.start_date, today)

    def test_allows_overnight_transport(self):
        request = make_request(departure_time="18:00", arrival_time="06:00")
        self.assertEqual(request.arrival_time, "06:00")


class TripQualityServiceTests(TestCase):
    def test_budget_is_calculated_deterministically(self):
        request = make_request()
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(
            plan, request, intercity_cost_per_person=50
        )

        self.assertEqual(result.budget.total_attractions, 60)
        self.assertEqual(result.budget.total_meals, 120)
        self.assertEqual(result.budget.total_hotels, 100)
        self.assertEqual(result.budget.intercity_transportation, 200)
        self.assertEqual(result.budget.local_transportation, 200)
        self.assertEqual(result.budget.total, 680)
        self.assertEqual(result.budget.per_person, 340)
        self.assertFalse(result.budget.over_budget)

    def test_marks_over_budget(self):
        request = make_request(budget_limit=100)
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)

        self.assertTrue(result.budget.over_budget)
        self.assertTrue(any("超出预算" in item for item in result.warnings))

    def test_removes_cross_day_duplicates_and_builds_timeline(self):
        request = make_request()
        first_day = make_day(0, 10)
        second_day = make_day(1, 20)
        second_day.attractions[0] = first_day.attractions[0].model_copy(deep=True)
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[first_day, second_day],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)

        assert len(result.days[0].attractions) == 1
        assert result.days[1].attractions == []
        assert any("移除重复景点" in item for item in result.warnings)
        assert result.days[0].schedule
        assert {item.item_type for item in result.days[0].schedule} >= {
            "meal", "transport", "attraction", "hotel"
        }

    def test_timeline_is_chronological_and_contains_break_buffer(self):
        request = make_request(origin_city="北京", city="北京")
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)
        schedule = result.days[0].schedule

        assert all(left.end_time <= right.start_time for left, right in zip(schedule, schedule[1:]))
        assert any(item.item_type == "break" for item in schedule)
        assert schedule[0].start_time == "07:45"

    def test_unknown_route_uses_estimated_journey_before_sightseeing(self):
        request = make_request(origin_city="武汉", city="陌生目的地")
        day0 = make_day(0, 10)
        day0.attractions = [
            day0.attractions[0].model_copy(update={"name": f"打卡点{i}", "visit_duration": 45})
            for i in range(3)
        ]
        plan = TripPlan(
            city=request.city, start_date=request.start_date, end_date=request.end_date,
            days=[day0, make_day(1, 20)], overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request, bind_meals=False)

        self.assertIsNotNone(result.intercity_journey)
        self.assertEqual(result.intercity_journey.source, "estimated")
        self.assertEqual(result.intercity_journey.arrival_time, "12:00")
        attraction_slots = [item for item in result.days[0].schedule if item.item_type == "attraction"]
        self.assertTrue(attraction_slots)
        self.assertTrue(all(item.start_time >= "12:45" for item in attraction_slots))
        self.assertGreaterEqual(len(result.days[0].attractions), 3)

    def test_first_day_sightseeing_starts_after_confirmed_intercity_arrival(self):
        request = make_request(departure_time="09:00", arrival_time="15:00")
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)
        schedule = result.days[0].schedule
        journey = result.intercity_journey

        self.assertIsNotNone(journey)
        self.assertFalse(journey.is_estimated)
        self.assertTrue(any(
            item.item_type == "transport"
            and item.start_time == "09:00"
            and item.end_time == "15:00"
            for item in schedule
        ))
        attraction_slots = [item for item in schedule if item.item_type == "attraction"]
        self.assertTrue(all(item.start_time >= "15:45" for item in attraction_slots))

    def test_late_arrival_does_not_schedule_lunch_in_the_evening(self):
        request = make_request(departure_time="12:00", arrival_time="18:00")
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)],
            overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)
        schedule = result.days[0].schedule

        self.assertFalse(any("午餐" in item.title for item in schedule))
        self.assertTrue(any("晚餐" in item.title for item in schedule))
        self.assertFalse(any(item.item_type == "attraction" for item in schedule))

    def test_overnight_outbound_and_return_use_day_offsets(self):
        request = make_request(
            departure_time="18:00", arrival_time="06:00",
            return_departure_time="19:00", return_arrival_time="01:00",
        )
        plan = TripPlan(
            city="北京", start_date=request.start_date, end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)], overall_suggestions="测试",
        )
        result = TripQualityService().finalize(plan, request)

        outbound = next(item for item in result.days[0].schedule if "跨城交通" in item.title)
        returning = next(item for item in result.days[-1].schedule if item.title.startswith("返程交通"))
        self.assertEqual((outbound.start_time, outbound.end_time, outbound.end_day_offset), ("18:00", "06:00", 1))
        self.assertEqual((returning.start_time, returning.end_time, returning.end_day_offset), ("19:00", "01:00", 1))
        self.assertTrue(all(int(item.start_time[:2]) < 24 and int(item.end_time[:2]) < 24 for day in result.days for item in day.schedule))

    def test_return_day_activities_finish_before_station_departure(self):
        request = make_request(
            departure_time="09:00", arrival_time="12:00",
            return_departure_time="16:00", return_arrival_time="21:00",
        )
        plan = TripPlan(
            city="北京", start_date=request.start_date, end_date=request.end_date,
            days=[make_day(0, 10), make_day(1, 20)], overall_suggestions="测试",
        )
        result = TripQualityService().finalize(plan, request)
        return_day = result.days[-1].schedule
        station_step = next(item for item in return_day if item.title.startswith("前往 返程"))
        attraction_steps = [item for item in return_day if item.item_type == "attraction"]
        self.assertTrue(all(item.end_time <= station_step.start_time for item in attraction_steps))
        self.assertFalse(any(item.item_type == "hotel" for item in return_day))

    def test_balances_sparse_last_day_when_capacity_is_available(self):
        start = datetime.now().date() + timedelta(days=30)
        request = make_request(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=2)).isoformat(),
            travel_days=3,
            origin_city="北京",
            city="北京",
        )
        days = [make_day(index, 0) for index in range(3)]
        for index, day in enumerate(days):
            day.date = (start + timedelta(days=index)).isoformat()
            day.hotel.location = Location(longitude=116.4, latitude=39.9)
        days[0].attractions = [
            Attraction(name=f"A{i}", address="测试", location=Location(longitude=116.40 + i * .01, latitude=39.9), visit_duration=90, description="测试")
            for i in range(4)
        ]
        days[1].attractions = [
            Attraction(name=f"B{i}", address="测试", location=Location(longitude=116.50 + i * .01, latitude=39.9), visit_duration=90, description="测试")
            for i in range(3)
        ]
        days[2].attractions = [
            Attraction(name=f"C{i}", address="测试", location=Location(longitude=116.60 + i * .01, latitude=39.9), visit_duration=90, description="测试")
            for i in range(2)
        ]
        plan = TripPlan(
            city="北京", start_date=request.start_date, end_date=request.end_date,
            days=days, overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)

        self.assertEqual(sum(len(day.attractions) for day in result.days), 9)
        self.assertGreaterEqual(len(result.days[-1].attractions), 3)
        self.assertLessEqual(max(len(day.attractions) for day in result.days) - min(len(day.attractions) for day in result.days), 1)
        self.assertTrue(any("重新分配" in warning for warning in result.warnings))

    def test_timeline_uses_leg_specific_commute_times(self):
        start = datetime.now().date() + timedelta(days=30)
        request = make_request(
            start_date=start.isoformat(), end_date=start.isoformat(), travel_days=1,
            origin_city="北京", city="北京",
        )
        day = make_day(0, 0)
        day.date = start.isoformat()
        day.hotel.location = Location(longitude=116.4, latitude=39.9)
        day.attractions = [
            Attraction(name="酒店附近", address="测试", location=Location(longitude=116.401, latitude=39.9), visit_duration=60, description="测试"),
            Attraction(name="较远片区", address="测试", location=Location(longitude=116.55, latitude=39.9), visit_duration=60, description="测试"),
        ]
        plan = TripPlan(
            city="北京", start_date=request.start_date, end_date=request.end_date,
            days=[day], overall_suggestions="测试",
        )

        result = TripQualityService().finalize(plan, request)
        commute_steps = [
            item.duration_minutes for item in result.days[0].schedule
            if item.item_type == "transport" and item.title.startswith("前往 ")
        ]

        self.assertEqual(len(commute_steps), 2)
        self.assertNotEqual(commute_steps[0], commute_steps[1])
        self.assertEqual(result.days[0].route_summary.start_anchor, "测试酒店")


class OverallSuggestionsTests(TestCase):
    """总体建议：finalize 收尾时重写为多条实用贴士（概览页编号列表的数据源）。"""

    def _plan_with_rain(self) -> TripPlan:
        from app.models.schemas import WeatherInfo

        request = make_request()
        plan = TripPlan(
            city="北京",
            start_date=request.start_date,
            end_date=request.end_date,
            days=[make_day(0, 60), make_day(1, 0)],
            overall_suggestions="这是为您规划的北京2日游行程。",
            weather_info=[
                WeatherInfo(
                    date=request.start_date,
                    day_weather="小雨",
                    night_weather="阴",
                    day_temp=18,
                    night_temp=8,
                    wind_direction="东北风",
                    wind_power="5-6级",
                )
            ],
        )
        return plan

    def test_llm_suggestions_win(self):
        from unittest import mock

        plan = self._plan_with_rain()
        raw = '["行程首日有雨，带好雨具", "早晚温差大，备一件外套", "景点门票建议提前购买"]'
        from app.config import get_settings
        with mock.patch.object(get_settings(), 'planning_tips_mode', 'llm'), mock.patch.object(
            TripQualityService, "_rule_based_suggestions", return_value="兜底"
        ) as fallback, mock.patch(
            "app.services.trip_quality_service._get_suggestions_llm"
        ) as llm_factory:
            llm_factory.return_value.invoke.return_value = raw
            result = trip_quality_service.refine_overall_suggestions(plan, make_request())
        self.assertEqual(result.split("\n")[0], "行程首日有雨，带好雨具")
        self.assertEqual(len(result.split("\n")), 3)
        fallback.assert_not_called()

    def test_llm_failure_falls_back_to_rules(self):
        from unittest import mock

        plan = self._plan_with_rain()
        with mock.patch(
            "app.services.trip_quality_service._get_suggestions_llm"
        ) as llm_factory:
            llm_factory.return_value.invoke.side_effect = RuntimeError("网络错误")
            result = trip_quality_service.refine_overall_suggestions(plan, make_request())
        tips = result.split("\n")
        self.assertTrue(any("降雨" in tip for tip in tips))
        self.assertTrue(any("风力较大" in tip for tip in tips))
        self.assertTrue(any("温差" in tip for tip in tips))
        self.assertTrue(any("收费景点" in tip for tip in tips))

    def test_llm_garbage_output_falls_back(self):
        from unittest import mock

        plan = self._plan_with_rain()
        with mock.patch(
            "app.services.trip_quality_service._get_suggestions_llm"
        ) as llm_factory:
            llm_factory.return_value.invoke.return_value = "抱歉我无法完成这个任务"
            result = trip_quality_service.refine_overall_suggestions(plan, make_request())
        self.assertIn("降雨", result)

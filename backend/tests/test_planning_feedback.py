"""结构化反馈与冻结边界的离线回归，不调用外部服务。"""
import unittest
from datetime import date, timedelta

from pydantic import ValidationError

from app.models.planning_feedback import PlanningRepairFeedback
from app.models.schemas import TripPlan, TripRequest, DayPlan, Attraction, Location, ScheduleItem
from app.services.planning_feedback_service import build_planning_feedback, validate_repair_scope


def fixture():
    start = date.today() + timedelta(days=7)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(2)]
    request = TripRequest(city="杭州", start_date=dates[0], end_date=dates[1], travel_days=2,
                          accommodation="舒适型酒店", transportation="公共交通")
    days = []
    for i, day_date in enumerate(dates):
        attraction = Attraction(stable_id=f"place-{i}", poi_id=f"poi-{i}", name=f"景点{i}",
            address="杭州", location=Location(longitude=120.1, latitude=30.2), visit_duration=90, description="介绍")
        day = DayPlan(date=day_date, day_index=i, description="一天", transportation="公共交通",
                      accommodation=request.accommodation, attractions=[attraction])
        if i == 1:
            day.schedule = [ScheduleItem(start_time="09:00", end_time="10:30", item_type="attraction",
                title=attraction.name, stable_id=attraction.stable_id, related_index=0, duration_minutes=90)]
        days.append(day)
    return TripPlan(city=request.city, start_date=dates[0], end_date=dates[1], days=days, overall_suggestions="建议"), request


class PlanningFeedbackTests(unittest.TestCase):
    def test_long_classic_visit_needs_review_even_with_a_reason(self):
        plan, request = fixture()
        day = plan.days[1]
        day.attractions[0].visit_duration = 360
        day.single_place_reason = '景区很大，需要一整天'
        feedback = build_planning_feedback(plan, request)
        self.assertTrue(any(w.type == 'single_place_review' and w.date == day.date
                            for w in feedback.soft_warnings))
        day.attractions[0].duration_source = 'user_confirmed'
        feedback = build_planning_feedback(plan, request)
        self.assertFalse(any(w.type == 'single_place_review' and w.date == day.date
                             for w in feedback.soft_warnings))

    def test_long_transit_does_not_hide_short_visit(self):
        plan, request = fixture()
        day = plan.days[1]
        day.attractions[0].visit_duration = 60
        day.schedule[0].duration_minutes = 60
        day.schedule[0].end_time = '10:00'
        day.schedule.append(ScheduleItem(start_time='10:00', end_time='13:00', item_type='transport', title='长途通勤', duration_minutes=180))
        feedback = build_planning_feedback(plan, request)
        warnings = {w.type for w in feedback.soft_warnings if w.date == day.date}
        self.assertIn('underfilled_day', warnings)
        self.assertIn('excessive_transit', warnings)
        self.assertEqual(feedback.day_metrics[day.date]['visit_minutes'], 60)

    def test_low_utilization_is_only_a_soft_warning_and_relaxed_pace_allows_it(self):
        plan, request = fixture()
        feedback = build_planning_feedback(plan, request)
        self.assertTrue(any(w.type == "underfilled_day" for w in feedback.soft_warnings))
        self.assertFalse(any(c.type == "underfilled_day" for c in feedback.conflicts))
        relaxed = build_planning_feedback(plan, request.model_copy(update={"pace": "relaxed"}))
        self.assertFalse(any(w.type == "underfilled_day" for w in relaxed.soft_warnings))

    def test_backtracking_is_explained_without_reordering_places(self):
        plan, request = fixture()
        first = plan.days[0].attractions[0]
        middle = first.model_copy(update={"stable_id": "far", "poi_id": "far", "name": "远处景点",
                                         "location": Location(longitude=120.3, latitude=30.2)})
        last = first.model_copy(update={"stable_id": "near", "poi_id": "near", "name": "附近景点",
                                       "location": Location(longitude=120.101, latitude=30.2)})
        plan.days[0].attractions.extend([middle, last])
        original_order = [a.stable_id for a in plan.days[0].attractions]
        feedback = build_planning_feedback(plan, request)
        self.assertTrue(any(w.type == "route_backtracking" for w in feedback.soft_warnings))
        self.assertEqual([a.stable_id for a in plan.days[0].attractions], original_order)

    def test_old_plan_reuses_schedule_identity_without_false_missing_place(self):
        plan, request = fixture()
        plan.days[1].attractions[0].stable_id = ""
        feedback = build_planning_feedback(plan, request)
        self.assertEqual([c.place_id for c in feedback.conflicts], ["place-0"])
        self.assertEqual(validate_repair_scope(plan, plan, feedback), [])

    def test_missing_schedule_generates_explicit_conflict_without_mutating_input(self):
        plan, request = fixture()
        before = plan.model_dump(mode="json")
        feedback = build_planning_feedback(plan, request)
        self.assertEqual([c.type for c in feedback.conflicts], ["unscheduled_place"])
        self.assertEqual(feedback.conflicts[0].place_id, "place-0")
        self.assertIsNone(feedback.conflicts[0].missing_minutes)
        self.assertEqual(feedback.editable_dates, [request.start_date])
        self.assertIn(request.end_date, feedback.frozen_days)
        self.assertEqual(plan.model_dump(mode="json"), before)

    def test_frozen_day_change_is_rejected_but_editable_day_change_is_allowed(self):
        plan, request = fixture()
        feedback = build_planning_feedback(plan, request)
        repaired = plan.model_copy(deep=True)
        repaired.days[0].attractions[0].visit_duration = 100
        self.assertEqual(validate_repair_scope(plan, repaired, feedback), [])
        repaired.days[1].hotel = None
        repaired.days[1].description = "模型顺便修改了另一天"
        self.assertEqual(validate_repair_scope(plan, repaired, feedback)[0].type, "frozen_change")

    def test_locked_place_and_global_data_are_not_editable(self):
        plan, request = fixture()
        feedback = build_planning_feedback(plan, request, editable_dates=[d.date for d in plan.days], locked_place_ids=["place-0"])
        repaired = plan.model_copy(deep=True)
        repaired.days[1].attractions.append(repaired.days[0].attractions.pop())
        self.assertTrue(any(c.type == "locked_place_change" for c in validate_repair_scope(plan, repaired, feedback)))
        repaired = plan.model_copy(deep=True)
        repaired.city = "北京"
        self.assertTrue(validate_repair_scope(plan, repaired, feedback))

    def test_contradictory_scope_and_unknown_ids_are_rejected(self):
        plan, request = fixture()
        feedback = build_planning_feedback(plan, request).model_dump()
        feedback["editable_dates"].append(request.end_date)
        with self.assertRaises(ValidationError):
            PlanningRepairFeedback.model_validate(feedback)
        with self.assertRaises(ValueError):
            build_planning_feedback(plan, request, locked_place_ids=["missing"])

    def test_overlapping_schedule_and_wrong_identity_are_detected(self):
        plan, request = fixture()
        plan.days[1].schedule_mode = "timed"
        plan.days[1].schedule.append(ScheduleItem(start_time="10:00", end_time="11:00", item_type="meal", title="午餐"))
        plan.days[1].schedule[0].stable_id = "wrong-id"
        types = [c.type for c in build_planning_feedback(plan, request).conflicts]
        self.assertIn("time_overlap", types)
        self.assertEqual(types.count("unscheduled_place"), 2)

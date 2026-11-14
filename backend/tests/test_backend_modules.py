"""模块重构回归：只用临时数据库、任务替身和模拟图源。"""

import asyncio
from datetime import date, timedelta
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import get_ident
from unittest import TestCase, mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.auth import AuthMiddleware
from app.api.routes import trip, poi
from app.infrastructure.database import connection
from app.models.schemas import PlanningJobResponse, TripPlan, TripRequest
from app.services import planning_application_service as application
from app.services.attraction_image_service import AttractionImageService
from app.services.plan_response_parser import PlanResponseError, plan_response_parser


def request_fixture():
    day = (date.today() + timedelta(days=30)).isoformat()
    return TripRequest(city="青岛", start_date=day, end_date=day, travel_days=1,
                       transportation="公共交通", accommodation="经济型酒店")


class BackendModuleTests(TestCase):
    def test_shared_database_commits_rolls_back_and_enables_foreign_keys(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "isolated.db"
            with connection(path) as db:
                self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                db.execute("CREATE TABLE items (value TEXT)")
                db.execute("INSERT INTO items VALUES ('kept')")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with self.assertRaises(ValueError):
                with connection(path) as db:
                    db.execute("INSERT INTO items VALUES ('rolled back')")
                    raise ValueError("取消本次写入")
            with connection(path) as db:
                self.assertEqual([r[0] for r in db.execute("SELECT value FROM items")], ["kept"])

    def test_parser_works_without_agent_and_reports_truncation(self):
        with self.assertRaisesRegex(PlanResponseError, "截断"):
            plan_response_parser.parse('{"days":[', request_fixture())
        cleaned = plan_response_parser._clean_json_string('{"url":"https://example.com", //说明\n}')
        self.assertIn("https://example.com", cleaned)

    def test_submission_copies_input_and_injects_server_memory(self):
        request = request_fixture()
        request.memory_context = "客户端内容"
        raw = SimpleNamespace(state=SimpleNamespace(user={"id": "owner"}))
        job = PlanningJobResponse(job_id="job", status="queued")
        with mock.patch.object(application, "submit_once", side_effect=lambda r, k, p, create: create("job")), \
             mock.patch.object(application, "require_planning_runtime"), \
             mock.patch.object(application, "enforce_planning_quota") as quota, \
             mock.patch.object(application, "memory_context", return_value="服务端偏好"), \
             mock.patch.object(application, "summary_for", return_value=("会话摘要", {})), \
             mock.patch.object(application.planning_job_service, "create_plan", return_value=job) as create:
            self.assertEqual(application.submit_plan(raw, request).job_id, "job")
        prepared = create.call_args.args[0]
        self.assertEqual(prepared.memory_context, "服务端偏好")
        self.assertEqual(prepared.conversation_context, "会话摘要")
        self.assertEqual(request.memory_context, "客户端内容")
        quota.assert_called_once_with("owner")

    def test_legacy_plan_uses_queue_and_exposes_confirmation_job(self):
        job = PlanningJobResponse(job_id="waiting-job", status="waiting_input", waiting_input={"type": "budget"})
        with mock.patch.object(application, "submit_plan", return_value=job) as submit:
            with self.assertRaises(HTTPException) as error:
                asyncio.run(trip.plan_trip(request_fixture(), SimpleNamespace()))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["job_id"], "waiting-job")
        submit.assert_called_once()

    def test_map_and_poi_require_login_before_query(self):
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        app.get("/api/map/poi")(lambda: {"unexpected": True})
        app.get("/api/poi/search")(lambda: {"unexpected": True})
        with TestClient(app) as client, mock.patch("app.api.auth.auth_service.session_user", return_value=None):
            self.assertEqual(client.get("/api/map/poi").status_code, 401)
            self.assertEqual(client.get("/api/poi/search").status_code, 401)

    def test_legacy_plan_returns_completed_queue_result(self):
        request = request_fixture()
        plan = TripPlan(city=request.city, start_date=request.start_date, end_date=request.end_date,
                        days=[], overall_suggestions="测试计划")
        queued = PlanningJobResponse(job_id="job", status="queued")
        completed = PlanningJobResponse(job_id="job", status="completed", data=plan)
        with mock.patch.object(application, "submit_plan", return_value=queued), \
             mock.patch.object(trip.planning_job_service, "get", return_value=completed) as read, \
             mock.patch.object(trip.asyncio, "sleep", new_callable=mock.AsyncMock):
            result = asyncio.run(trip.plan_trip(request, SimpleNamespace()))
        self.assertEqual(result.data.city, "青岛")
        read.assert_called_once_with("job")

    def test_health_reads_status_without_initializing_agent(self):
        for ready in (True, False):
            with self.subTest(ready=ready), mock.patch("app.services.runtime_service.runtime_status", return_value={"ready": ready}):
                if ready:
                    self.assertEqual(asyncio.run(trip.health_check())["status"], "healthy")
                else:
                    with self.assertRaises(HTTPException) as error:
                        asyncio.run(trip.health_check())
                    self.assertEqual(error.exception.status_code, 503)

    def test_failed_image_fetch_is_not_stuck_in_memory(self):
        service = AttractionImageService()
        with mock.patch("app.services.attraction_image_service._image_cache_read", return_value=None), \
             mock.patch("app.services.attraction_image_service._image_cache_write"), \
             mock.patch.object(service, "_get_attraction_image_parallel", new_callable=mock.AsyncMock, return_value=None) as fetch:
            self.assertIsNone(service.get_attraction_image("栈桥", "青岛"))
            self.assertIsNone(service.get_attraction_image("栈桥", "青岛"))
        self.assertEqual(fetch.await_count, 2)

    def test_legacy_poi_search_runs_outside_event_loop_thread(self):
        main_thread = get_ident()
        query_threads = []

        def search(*args):
            query_threads.append(get_ident())
            return []

        service = SimpleNamespace(search_poi=search)
        with mock.patch.object(poi, "get_amap_service", return_value=service):
            result = asyncio.run(poi.search_poi("栈桥", "青岛"))
        self.assertEqual(result["data"], [])
        self.assertEqual(len(query_threads), 1)
        self.assertNotEqual(query_threads[0], main_thread)

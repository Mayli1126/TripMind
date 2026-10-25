"""阶段 D Redis 双轨测试：用 fakeredis 覆盖配额 / Stream 队列 / Recorder / Worker。

Redis 未启用（redis_enabled=False）时这些路径完全不参与运行，
因此这里直接替换 redis_client._client 为 fakeredis 实例做纯逻辑验证。
"""

import json
import unittest
from datetime import date, timedelta
from unittest import mock

import fakeredis

from app.infrastructure import redis_client
from app.infrastructure.redis_client import (
    ack_job,
    append_job_event,
    dequeue_jobs,
    enqueue_job,
    get_job_state,
    quota_check_and_incr,
    read_job_events,
    set_job_state,
)
from app.models.schemas import TripPlan
from app.services import planning_worker
from app.services.attraction_image_service import (
    _image_cache_read,
    _image_cache_write,
)
from app.services.planning_job_service import RedisJobRecorder


class RedisStageDTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_client = redis_client._client
        self._orig_group_ready = redis_client._group_ready
        redis_client._client = fakeredis.FakeRedis(decode_responses=True)
        redis_client._group_ready = False

    def tearDown(self):
        redis_client._client = self._orig_client
        redis_client._group_ready = self._orig_group_ready

    # -- 公共构造 ---------------------------------------------------------

    def _plan(self) -> TripPlan:
        return TripPlan(
            city="北京",
            start_date="2026-12-01",
            end_date="2026-12-01",
            days=[],
            overall_suggestions="测试计划",
        )

    def _request(self) -> dict:
        start = (date.today() + timedelta(days=30)).isoformat()
        return {
            "origin_city": "上海",
            "city": "北京",
            "start_date": start,
            "end_date": start,
            "travel_days": 1,
            "transportation": "high_speed_train",
            "accommodation": "经济型酒店",
        }

    # -- 配额 -------------------------------------------------------------

    def test_quota_check_and_incr_enforces_limit(self):
        self.assertEqual(quota_check_and_incr("client-a", 2), (True, 1))
        self.assertEqual(quota_check_and_incr("client-a", 2), (True, 2))
        # 第 3 次超限被拒
        self.assertEqual(quota_check_and_incr("client-a", 2), (False, 2))
        # 不同 client 互不影响
        self.assertEqual(quota_check_and_incr("client-b", 2), (True, 1))

    def test_quota_unlimited_passes_through(self):
        self.assertEqual(quota_check_and_incr("client-a", 0), (True, 0))
        # 不限流时不产生计数键
        today = date.today().isoformat()
        self.assertIsNone(redis_client._client.get(f"quota:client-a:{today}"))

    # -- Stream 队列 ------------------------------------------------------

    def test_stream_enqueue_dequeue_ack_roundtrip(self):
        enqueue_job("job-1", "plan", {"request": self._request()})
        entries = dequeue_jobs(block_ms=50)
        self.assertEqual(len(entries), 1)
        message_id, fields = entries[0]
        self.assertEqual(fields["job_id"], "job-1")
        self.assertEqual(fields["type"], "plan")
        self.assertEqual(
            json.loads(fields["payload"])["request"]["city"], "北京"
        )
        ack_job(message_id)
        # 已 ACK 的消息不会再次被消费
        self.assertEqual(dequeue_jobs(block_ms=50), [])

    def test_job_state_and_events_roundtrip(self):
        set_job_state(
            "job-2",
            status="queued",
            progress=0,
            agents={},
            metrics={"queue_wait_ms": 0},
        )
        state = get_job_state("job-2")
        self.assertEqual(state["status"], "queued")
        self.assertEqual(json.loads(state["agents"]), {})

        append_job_event("job-2", {"sequence": 0, "status": "queued"})
        append_job_event("job-2", {"sequence": 1, "status": "running"})
        events = read_job_events("job-2", 0)
        self.assertEqual([event["sequence"] for event in events], [0, 1])
        # 增量拉取：只取 sequence >= 1
        self.assertEqual(
            [event["sequence"] for event in read_job_events("job-2", 1)], [1]
        )

    # -- RedisJobRecorder -------------------------------------------------

    def test_recorder_lifecycle_and_response(self):
        recorder = RedisJobRecorder("job-3")
        recorder.init("plan")
        recorder.callback()(20, "research", "研究中", "attractions", "running")
        recorder.callback()(60, "research", "研究完成", "attractions", "completed")
        recorder.complete(self._plan())

        response = recorder.response()
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.progress, 100)
        self.assertIsNotNone(response.data)
        self.assertEqual(response.data.city, "北京")
        self.assertEqual(response.agents["attractions"].status, "completed")
        self.assertEqual(response.metrics.degraded_agents, [])
        self.assertIsNotNone(response.metrics.total_duration_ms)

        events = read_job_events("job-3", 0)
        self.assertEqual(events[0]["status"], "queued")
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(
            [event["sequence"] for event in events], list(range(len(events)))
        )

    # -- Worker -----------------------------------------------------------

    def test_worker_process_once_completes_job(self):
        enqueue_job("job-4", "plan", {"request": self._request(), "attempt": 0})
        plan = self._plan()
        with mock.patch(
            "app.graphs.planning_graph.run_planning_graph", return_value=plan
        ) as run_mock:
            handled = planning_worker.process_once(block_ms=50)

        self.assertEqual(handled, 1)
        run_mock.assert_called_once()
        state = get_job_state("job-4")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(
            TripPlan.model_validate_json(state["data"]).city, "北京"
        )
        # 任务已 XACK，不会重复消费
        self.assertEqual(dequeue_jobs(block_ms=50), [])

    def test_worker_failure_schedules_retry(self):
        enqueue_job("job-5", "plan", {"request": self._request(), "attempt": 0})
        with mock.patch(
            "app.graphs.planning_graph.run_planning_graph",
            side_effect=RuntimeError("boom"),
        ):
            planning_worker.process_once(block_ms=50)

        state = get_job_state("job-5")
        self.assertEqual(state["status"], "retrying")
        # 第一次失败：进入重试 ZSET（60s 后到期），未进死信
        retries = redis_client._client.zrange("trip:retry", 0, -1)
        self.assertEqual(len(retries), 1)
        item = json.loads(retries[0])
        self.assertEqual(item["job_id"], "job-5")
        self.assertEqual(item["attempt"], 1)
        self.assertEqual(redis_client._client.xlen("trip:jobs:dead"), 0)

    def test_worker_failure_exhausted_goes_dead_letter(self):
        enqueue_job("job-6", "plan", {"request": self._request(), "attempt": 3})
        with mock.patch(
            "app.graphs.planning_graph.run_planning_graph",
            side_effect=RuntimeError("boom"),
        ):
            planning_worker.process_once(block_ms=50)

        state = get_job_state("job-6")
        self.assertEqual(state["status"], "failed")
        self.assertIn("boom", state["error"])
        dead = redis_client._client.xrange("trip:jobs:dead")
        self.assertEqual(len(dead), 1)
        self.assertEqual(dead[0][1]["job_id"], "job-6")
        self.assertEqual(redis_client._client.zcard("trip:retry"), 0)

    # -- 图片缓存（阶段 F 起点） -------------------------------------------

    def test_attraction_image_cache_roundtrip(self):
        result = {
            "photo_url": "https://example.com/gugong.jpg",
            "source": "高德地图",
            "match_type": "title_match",
        }
        self.assertIsNone(_image_cache_read("故宫", "北京"))
        _image_cache_write("故宫", "北京", result)
        cached = _image_cache_read("故宫", "北京")
        self.assertEqual(cached["photo_url"], "https://example.com/gugong.jpg")

        # 负结果(None)不回写，不会覆盖已有缓存
        _image_cache_write("故宫", "北京", None)
        self.assertEqual(
            _image_cache_read("故宫", "北京")["photo_url"],
            "https://example.com/gugong.jpg",
        )


if __name__ == "__main__":
    unittest.main()

"""健身 HTTP 主接缝：记录一次训练、最近记录。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from hpa import store, web
from hpa.config import Config

TOKEN = "test-token"


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


class FitnessWebCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "vault").mkdir()
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)
        self.server = web.make_server(self.cfg, host="127.0.0.1", port=0, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _post(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json", "X-HPA-Token": TOKEN},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _state(self) -> dict:
        req = urllib.request.Request(self.base + "/api/state", headers={"X-HPA-Token": TOKEN})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def _record(self, exercises: list[dict], note: str = "") -> tuple[int, dict]:
        return self._post(
            "/api/fitness/record",
            {"kind": "workout", "performed_on": _today(), "note": note, "exercises": exercises},
        )


class RecordWorkoutTest(FitnessWebCase):
    def test_record_squat_five_sets_and_see_it_in_state(self) -> None:
        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "workout",
                "performed_on": _today(),
                "started_at": "18:30",
                "note": "状态不错",
                "exercises": [
                    {"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}] * 5}
                ],
            },
        )
        self.assertEqual(code, 200, data)
        self.assertIn("id", data)

        fitness = self._state()["fitness"]
        self.assertEqual(len(fitness["recent_workouts"]), 1)
        workout = fitness["recent_workouts"][0]
        self.assertEqual(workout["id"], data["id"])
        self.assertEqual(workout["note"], "状态不错")
        self.assertEqual(workout["started_at"], "18:30")
        squat = workout["exercises"][0]
        self.assertEqual(squat["exercise"], "squat")
        self.assertEqual(len(squat["sets"]), 5)
        self.assertEqual([s["reps"] for s in squat["sets"]], [5] * 5)
        self.assertEqual(squat["volume"], 2000)
        self.assertEqual(squat["volume_kind"], "kg")

    def test_bodyweight_counts_reps_and_mixes_with_weighted(self) -> None:
        code, data = self._record(
            [
                {"exercise": "bench_press", "sets": [{"weight_kg": 60, "reps": 5}] * 5},
                {"exercise": "pull_up", "sets": [{"reps": 8}] * 3},
            ]
        )
        self.assertEqual(code, 200, data)
        workout = self._state()["fitness"]["recent_workouts"][0]
        by_exercise = {item["exercise"]: item for item in workout["exercises"]}
        self.assertEqual(by_exercise["bench_press"]["volume"], 1500)
        self.assertEqual(by_exercise["bench_press"]["volume_kind"], "kg")
        self.assertEqual(by_exercise["pull_up"]["volume"], 24)
        self.assertEqual(by_exercise["pull_up"]["volume_kind"], "reps")
        self.assertIsNone(by_exercise["pull_up"]["sets"][0]["weight_kg"])

    def test_rejects_unknown_exercise_bad_reps_and_future_date(self) -> None:
        code, data = self._record([{"exercise": "curl", "sets": [{"weight_kg": 10, "reps": 8}]}])
        self.assertEqual(code, 400)
        self.assertIn("未知动作", data["error"])

        code, data = self._record([{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 0}]}])
        self.assertEqual(code, 400)
        self.assertIn("次数", data["error"])

        tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "workout",
                "performed_on": tomorrow,
                "exercises": [{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}]}],
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("不能晚于今天", data["error"])
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])

    def test_delete_requires_confirm_and_removes_sets(self) -> None:
        code, data = self._record(
            [{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}] * 5}]
        )
        self.assertEqual(code, 200, data)
        workout_id = data["id"]

        code, data = self._post(f"/api/fitness/workouts/{workout_id}/delete", {})
        self.assertEqual(code, 400)
        self.assertIn("确认", data["error"])
        code, _ = self._post(f"/api/fitness/workouts/{workout_id}/delete", {"confirm": "false"})
        self.assertEqual(code, 400)
        code, _ = self._post(f"/api/fitness/workouts/{workout_id}/delete", {"confirm": 1})
        self.assertEqual(code, 400)
        self.assertEqual(len(self._state()["fitness"]["recent_workouts"]), 1)

        code, data = self._post(
            f"/api/fitness/workouts/{workout_id}/delete", {"confirm": True}
        )
        self.assertEqual(code, 200, data)
        self.assertEqual(data["status"], "deleted")
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0], 0)
        finally:
            conn.close()

        code, _ = self._post(f"/api/fitness/workouts/{workout_id}/delete", {"confirm": True})
        self.assertEqual(code, 400)

    def test_rejects_fractional_reps_non_numeric_weight_and_bad_time(self) -> None:
        code, data = self._record(
            [{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5.5}]}]
        )
        self.assertEqual(code, 400)
        self.assertIn("整数", data["error"])

        code, data = self._record(
            [{"exercise": "squat", "sets": [{"weight_kg": "80", "reps": 5}]}]
        )
        self.assertEqual(code, 400)
        self.assertIn("数字", data["error"])

        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "workout",
                "performed_on": _today(),
                "started_at": "25:00",
                "exercises": [{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}]}],
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("开始时间", data["error"])
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])


if __name__ == "__main__":
    unittest.main()

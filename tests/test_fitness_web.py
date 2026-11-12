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


class WeightAndProfileTest(FitnessWebCase):
    def test_weight_keeps_one_row_per_day_and_overwrites(self) -> None:
        code, data = self._post("/api/fitness/record", {"kind": "weight", "weight_kg": 75.0})
        self.assertEqual(code, 200, data)
        code, data = self._post("/api/fitness/record", {"kind": "weight", "weight_kg": 74.5})
        self.assertEqual(code, 200, data)

        weights = self._state()["fitness"]["weights"]
        self.assertEqual(len(weights), 1)
        self.assertEqual(weights[0]["logged_on"], _today())
        self.assertEqual(weights[0]["weight_kg"], 74.5)

    def test_profile_saves_four_lines_and_preserves_free_text(self) -> None:
        memory = self.root / "memory"
        memory.mkdir()
        profile_path = memory / "training-profile.md"
        profile_path.write_text(
            "目标：增肌\n每周可练：3\n器械：健身房\n伤病：无\n\n"
            "备注：膝盖旧伤，深蹲注意。\n伤病：膝盖旧伤，深蹲注意。\n",
            encoding="utf-8",
        )

        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "profile",
                "goal": "力量",
                "days_per_week": 4,
                "equipment": "健身房 + 杠铃",
                "injuries": "无",
            },
        )
        self.assertEqual(code, 200, data)

        profile = self._state()["fitness"]["profile"]
        self.assertTrue(profile["complete"])
        self.assertEqual(profile["missing"], [])
        self.assertEqual(profile["goal"], "力量")
        self.assertEqual(profile["days_per_week"], 4)
        self.assertEqual(profile["equipment"], "健身房 + 杠铃")
        self.assertEqual(profile["injuries"], "无")

        text = profile_path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines[0], "目标：力量")
        self.assertEqual(lines[1], "每周可练：4")
        self.assertEqual(lines[2], "器械：健身房 + 杠铃")
        self.assertEqual(lines[3], "伤病：无")
        self.assertIn("备注：膝盖旧伤，深蹲注意。", text)
        self.assertIn("伤病：膝盖旧伤，深蹲注意。", text)
        self.assertEqual(text.count("伤病："), 2)
        self.assertEqual(text.count("目标："), 1)

    def test_missing_profile_line_marks_incomplete(self) -> None:
        memory = self.root / "memory"
        memory.mkdir()
        (memory / "training-profile.md").write_text(
            "目标：力量\n器械：健身房\n\n自由备注\n", encoding="utf-8"
        )

        profile = self._state()["fitness"]["profile"]
        self.assertFalse(profile["complete"])
        self.assertEqual(profile["goal"], "力量")
        self.assertIsNone(profile["days_per_week"])
        self.assertIn("每周可练", profile["missing"])
        self.assertIn("伤病", profile["missing"])
        self.assertNotIn("目标", profile["missing"])

    def test_invalid_profile_is_rejected_and_file_unchanged(self) -> None:
        memory = self.root / "memory"
        memory.mkdir()
        profile_path = memory / "training-profile.md"
        profile_path.write_text(
            "目标：力量\n每周可练：3\n器械：健身房\n伤病：无\n", encoding="utf-8"
        )
        before = profile_path.read_text(encoding="utf-8")

        code, data = self._post(
            "/api/fitness/record",
            {"kind": "profile", "goal": "力量", "days_per_week": 0, "equipment": "健身房", "injuries": "无"},
        )
        self.assertEqual(code, 400)
        self.assertIn("每周可练", data["error"])
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

        code, data = self._post(
            "/api/fitness/record",
            {"kind": "profile", "goal": "随便", "days_per_week": 3, "equipment": "健身房", "injuries": "无"},
        )
        self.assertEqual(code, 400)
        self.assertIn("目标", data["error"])
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "profile",
                "goal": "力量",
                "days_per_week": 3,
                "equipment": "健身房\n目标：随便",
                "injuries": "无",
            },
        )
        self.assertEqual(code, 400)
        self.assertIn("换行", data["error"])
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

    def test_rejects_out_of_range_weight_and_future_date(self) -> None:
        for bad in (10, 301, "75"):
            code, data = self._post("/api/fitness/record", {"kind": "weight", "weight_kg": bad})
            self.assertEqual(code, 400, (bad, data))

        tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat()
        code, data = self._post(
            "/api/fitness/record", {"kind": "weight", "weight_kg": 75, "logged_on": tomorrow}
        )
        self.assertEqual(code, 400)
        self.assertIn("不能晚于今天", data["error"])
        self.assertEqual(self._state()["fitness"]["weights"], [])

    def test_corrupt_profile_degrades_instead_of_breaking_state(self) -> None:
        memory = self.root / "memory"
        memory.mkdir()
        (memory / "training-profile.md").write_bytes(b"\xff\xfe\x00bad")

        profile = self._state()["fitness"]["profile"]
        self.assertFalse(profile["complete"])
        self.assertEqual(profile["missing"], ["目标", "每周可练", "器械", "伤病"])

    def test_invalid_profile_does_not_create_the_file(self) -> None:
        profile_path = self.root / "memory" / "training-profile.md"
        code, data = self._post(
            "/api/fitness/record",
            {"kind": "profile", "goal": "力量", "days_per_week": 0, "equipment": "健身房", "injuries": "无"},
        )
        self.assertEqual(code, 400)
        self.assertFalse(profile_path.exists())
        self.assertFalse(profile_path.parent.exists())


if __name__ == "__main__":
    unittest.main()

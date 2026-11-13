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

from hpa import llm, store, web
from hpa.config import Config

TOKEN = "test-token"


class FakeLLM:
    def __init__(self, reply: str = "", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _no_llm(cfg):
    raise llm.LLMError("no key")


class FitnessWebCase(unittest.TestCase):
    llm_factory = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "vault").mkdir()
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)
        self.server = web.make_server(
            self.cfg,
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            llm_factory=self.llm_factory,
        )
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


class QuickInputTest(FitnessWebCase):
    def setUp(self) -> None:
        self.fake = FakeLLM()
        self.llm_factory = lambda cfg: self.fake
        super().setUp()
        prompts = self.root / "prompts"
        prompts.mkdir()
        source = Path(__file__).resolve().parents[1] / "prompts" / "fitness-parse.md"
        (prompts / "fitness-parse.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    def _parse(self, text: str) -> tuple[int, dict]:
        return self._post("/api/fitness/parse", {"text": text})

    def test_local_squat_parses_without_model_or_write(self) -> None:
        code, data = self._parse("深蹲 80kg 5组5次")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "rules")
        self.assertEqual(data["uncertain"], [])
        exercise = data["draft"]["exercises"][0]
        self.assertEqual(exercise["exercise"], "squat")
        self.assertEqual(len(exercise["sets"]), 5)
        for item in exercise["sets"]:
            self.assertEqual(item["weight_kg"], 80.0)
            self.assertEqual(item["reps"], 5)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])

    def test_default_weight_reps_sets_shape_parses_locally(self) -> None:
        code, data = self._parse("深蹲 80×5×5")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "rules")
        exercise = data["draft"]["exercises"][0]
        self.assertEqual(exercise["exercise"], "squat")
        self.assertEqual(len(exercise["sets"]), 5)
        for item in exercise["sets"]:
            self.assertEqual(item["weight_kg"], 80.0)
            self.assertEqual(item["reps"], 5)
        self.assertEqual(self.fake.calls, [])

    def test_local_common_spellings(self) -> None:
        cases = [
            ("平板卧推 60 5x5", "bench_press", 5, 60.0, 5),
            ("引体 8x3", "pull_up", 3, None, 8),
            ("硬拉 100kg 3×5", "deadlift", 5, 100.0, 3),
            ("二头弯举 20 4组8次", "barbell_curl", 4, 20.0, 8),
        ]
        for text, key, sets, weight, reps in cases:
            with self.subTest(text=text):
                code, data = self._parse(text)
                self.assertEqual(code, 200, data)
                self.assertEqual(data["parser"], "rules", data)
                exercise = data["draft"]["exercises"][0]
                self.assertEqual(exercise["exercise"], key)
                self.assertEqual(len(exercise["sets"]), sets)
                for item in exercise["sets"]:
                    self.assertEqual(item["weight_kg"], weight)
                    self.assertEqual(item["reps"], reps)
        self.assertEqual(self.fake.calls, [])

    def test_local_invalid_value_reports_without_calling_model(self) -> None:
        code, data = self._parse("深蹲 80kg 0组5次")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "none")
        self.assertIn("组数", data["message"])
        self.assertEqual(self.fake.calls, [])

    def test_local_failure_falls_back_to_model(self) -> None:
        self.fake.reply = json.dumps(
            {
                "exercises": [
                    {"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}] * 5}
                ],
                "uncertain": ["weight_kg"],
            }
        )
        code, data = self._parse("昨天蹲了五组五次，大概八十公斤")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "llm")
        self.assertEqual(data["uncertain"], ["weight_kg"])
        self.assertEqual(len(data["draft"]["exercises"][0]["sets"]), 5)
        self.assertEqual(len(self.fake.calls), 1)
        system, user = self.fake.calls[0]
        self.assertIn("bench_press", system)
        self.assertIn("昨天蹲了五组五次", user)
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])

    def test_multiple_exercises_in_one_sentence_is_not_silently_dropped(self) -> None:
        self.fake.reply = json.dumps(
            {
                "exercises": [
                    {"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}] * 5},
                    {"exercise": "bench_press", "sets": [{"weight_kg": 60, "reps": 5}] * 5},
                ],
                "uncertain": [],
            }
        )
        code, data = self._parse("深蹲 80kg 5组5次，平板卧推 60kg 5组5次")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "llm")
        self.assertEqual([item["exercise"] for item in data["draft"]["exercises"]], ["squat", "bench_press"])

    def test_model_error_returns_parser_none(self) -> None:
        self.fake.error = llm.LLMError("缺少 LLM 密钥")
        code, data = self._parse("昨天练了腿")
        self.assertEqual(code, 200, data)
        self.assertEqual(data["parser"], "none")
        self.assertIn("手动", data["message"])
        self.assertEqual(self._state()["fitness"]["recent_workouts"], [])

    def test_bad_model_json_and_unknown_exercise_degrade(self) -> None:
        for reply in (
            "我觉得是深蹲",
            json.dumps({"exercises": [{"exercise": "leg_press", "sets": []}], "uncertain": []}),
        ):
            with self.subTest(reply=reply):
                self.fake.reply = reply
                code, data = self._parse("昨天练了腿")
                self.assertEqual(code, 200, data)
                self.assertEqual(data["parser"], "none")

    def test_empty_text_is_rejected(self) -> None:
        code, data = self._parse("   ")
        self.assertEqual(code, 400)
        self.assertIn("请输入", data["error"])

    def test_parsed_draft_saves_like_manual_entry(self) -> None:
        code, parsed = self._parse("深蹲 80kg 5组5次")
        self.assertEqual(code, 200, parsed)
        exercise = parsed["draft"]["exercises"][0]
        code, data = self._record(
            [{"exercise": exercise["exercise"], "sets": exercise["sets"]}]
        )
        self.assertEqual(code, 200, data)
        workout = self._state()["fitness"]["recent_workouts"][0]
        self.assertEqual(workout["exercises"][0]["exercise"], "squat")
        self.assertEqual(len(workout["exercises"][0]["sets"]), 5)
        self.assertEqual(workout["exercises"][0]["volume"], 2000)
        self.assertEqual(workout["exercises"][0]["volume_kind"], "kg")


class AdviceCase(FitnessWebCase):
    llm_factory = staticmethod(_no_llm)

    def _seed(
        self,
        days_ago: int,
        exercise: str,
        weight: float,
        reps: int,
        sets: int,
        note: str = "",
    ) -> tuple[int, dict]:
        day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=days_ago)).isoformat()
        return self._post(
            "/api/fitness/record",
            {
                "kind": "workout",
                "performed_on": day,
                "started_at": "18:00",
                "note": note,
                "exercises": [
                    {"exercise": exercise, "sets": [{"weight_kg": weight, "reps": reps}] * sets}
                ],
            },
        )

    def _profile(self, days_per_week: int = 3) -> None:
        code, data = self._post(
            "/api/fitness/record",
            {
                "kind": "profile",
                "goal": "力量",
                "days_per_week": days_per_week,
                "equipment": "健身房",
                "injuries": "无",
            },
        )
        self.assertEqual(code, 200, data)

    def _advice(self) -> tuple[int, dict]:
        return self._post("/api/fitness/advice")

class AdviceTest(AdviceCase):
    def test_four_weeks_advice_has_evidence_and_persists(self) -> None:
        self._profile(3)
        for days_ago in (21, 14, 7, 0):
            code, data = self._seed(days_ago, "squat", 80, 5, 5)
            self.assertEqual(code, 200, data)

        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(advice["llm_used"])
        self.assertGreaterEqual(len(advice["items"]), 1)
        self.assertLessEqual(len(advice["items"]), 3)
        rules = {item["rule"] for item in advice["items"]}
        self.assertIn("frequency", rules)
        self.assertIn("stagnation", rules)
        self.assertNotIn("volume", rules)

        frequency = next(item for item in advice["items"] if item["rule"] == "frequency")
        self.assertIn("3 次/周", frequency["evidence"])
        stagnation = next(item for item in advice["items"] if item["rule"] == "stagnation")
        self.assertIn("2.0t", stagnation["evidence"])
        for item in advice["items"]:
            self.assertTrue(item["workout_ids"])
            self.assertTrue(any(char.isdigit() for char in item["evidence"]))
            self.assertTrue(item["conclusion"])
            self.assertTrue(item["action"])

        state = self._state()["fitness"]
        self.assertEqual(state["last_advice"]["generated_at"], advice["generated_at"])

    def test_one_workout_is_insufficient_not_invented(self) -> None:
        self._profile(3)
        self._seed(0, "squat", 80, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertEqual(advice["items"], [])
        self.assertTrue(any("至少记录 2 天" in note for note in advice["insufficient"]))

    def test_same_part_within_48h_flags_frequency(self) -> None:
        self._profile(2)
        self._seed(1, "bench_press", 60, 5, 5)
        self._seed(0, "bench_press", 60, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        frequency = next(item for item in advice["items"] if item["rule"] == "frequency")
        self.assertIn("24", frequency["evidence"])
        self.assertNotIn("画像目标", frequency["evidence"])

    def test_same_part_after_48h_without_weekly_gap_has_no_frequency(self) -> None:
        self._profile(2)
        self._seed(3, "bench_press", 60, 5, 5)
        self._seed(0, "bench_press", 60, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(any(item["rule"] == "frequency" for item in advice["items"]))

    def test_two_week_volume_decline_is_reported(self) -> None:
        self._profile(3)
        self._seed(21, "squat", 80, 5, 5)
        self._seed(14, "squat", 80, 5, 5)
        self._seed(7, "squat", 68, 5, 5)
        self._seed(0, "squat", 56, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        volume = next(item for item in advice["items"] if item["rule"] == "volume")
        self.assertIn("1.4t", volume["evidence"])
        self.assertIn("下降", volume["evidence"])

    def test_flat_volume_is_left_to_stagnation(self) -> None:
        self._profile(3)
        for days_ago in (21, 14, 7, 0):
            self._seed(days_ago, "squat", 80, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(any(item["rule"] == "volume" for item in advice["items"]))

    def test_body_weight_is_not_part_of_advice(self) -> None:
        self._profile(3)
        code, data = self._post(
            "/api/fitness/record",
            {"kind": "weight", "weight_kg": 77.3},
        )
        self.assertEqual(code, 200, data)
        for days_ago in (21, 14, 7, 0):
            self._seed(days_ago, "squat", 80, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertNotIn("77.3", json.dumps(advice, ensure_ascii=False))

    def test_notes_never_reach_advice(self) -> None:
        self._profile(3)
        for days_ago in (14, 7, 0):
            self._seed(days_ago, "squat", 80, 5, 5, note="肩痛，别加重")
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertNotIn("肩痛", json.dumps(advice, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()


class AdviceRegressionTest(AdviceCase):
    def test_empty_current_week_does_not_make_an_item_without_ids(self) -> None:
        self._profile(3)
        for days_ago in (16, 12, 8):
            self._seed(days_ago, "squat", 80, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(any(item["rule"] == "frequency" for item in advice["items"]))
        self.assertTrue(any("本周还没有训练记录" in note for note in advice["insufficient"]))

    def test_single_week_spike_is_reported_even_if_previous_week_is_empty(self) -> None:
        self._profile(3)
        self._seed(8, "bench_press", 60, 5, 5)
        self._seed(3, "bench_press", 60, 5, 5)
        self._seed(0, "bench_press", 60, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        volume = next(item for item in advice["items"] if item["rule"] == "volume")
        self.assertIn("上升", volume["evidence"])

    def test_stagnation_accepts_a_dip_and_recover_week(self) -> None:
        self._profile(3)
        self._seed(14, "squat", 100, 5, 5)
        self._seed(7, "squat", 95, 5, 5)
        self._seed(0, "squat", 100, 5, 5)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        stagnation = next(item for item in advice["items"] if item["rule"] == "stagnation")
        self.assertIn("没有提升", stagnation["evidence"])

    def test_profile_gap_survives_many_exercise_notes(self) -> None:
        exercises = [
            {"exercise": key, "sets": [{"weight_kg": None if key in ("pull_up", "hanging_leg_raise") else 40, "reps": 5}] * 3}
            for key in ("bench_press", "close_grip_bench", "pull_up", "barbell_curl", "hanging_leg_raise", "squat", "deadlift")
        ]
        for days_ago in (1, 0):
            day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=days_ago)).isoformat()
            code, data = self._post(
                "/api/fitness/record",
                {"kind": "workout", "performed_on": day, "started_at": "18:00", "exercises": exercises},
            )
            self.assertEqual(code, 200, data)
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertTrue(any("训练画像还缺" in note for note in advice["insufficient"]))


class WordingTest(AdviceCase):
    def setUp(self) -> None:
        self.client = FakeLLM()
        self.llm_factory = lambda cfg: self.client
        super().setUp()
        (self.root / "rules").mkdir()
        (self.root / "rules" / "fitness-advice.md").write_text(
            "健身建议规则：优先频率，其次容量，最后停滞。\n", encoding="utf-8"
        )
        (self.root / "prompts").mkdir()
        (self.root / "prompts" / "fitness-advice.md").write_text(
            "只输出 JSON。\n", encoding="utf-8"
        )

    def _weeks(self, note: str = "") -> None:
        self._profile(3)
        for days_ago in (21, 14, 7, 0):
            code, data = self._seed(days_ago, "squat", 80, 5, 5, note=note)
            self.assertEqual(code, 200, data)

    def _baseline(self) -> dict:
        original = self.server.llm_factory

        def no_key(cfg):
            raise llm.LLMError("no key")

        self.server.llm_factory = no_key
        try:
            code, advice = self._advice()
        finally:
            self.server.llm_factory = original
        self.assertEqual(code, 200, advice)
        return advice

    def _reply(self, order: list[str], texts: dict[str, tuple[str, str]]) -> None:
        payload = {
            "order": order,
            "texts": {
                rule: {"conclusion": conclusion, "action": action}
                for rule, (conclusion, action) in texts.items()
            },
        }
        self.client.reply = json.dumps(payload, ensure_ascii=False)

    def test_model_reorders_and_rewrites_but_evidence_is_unchanged(self) -> None:
        self._weeks(note="肩痛，别加重")
        baseline = self._baseline()
        self._reply(
            ["stagnation", "frequency"],
            {
                "stagnation": ("深蹲最近没有进步。", "下次保持重量，多做一次。"),
                "frequency": ("这周练得少了些。", "下周补一次全身训练。"),
            },
        )
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertTrue(advice["llm_used"])
        self.assertEqual([item["rule"] for item in advice["items"]], ["stagnation", "frequency"])
        base_evidence = {item["rule"]: item["evidence"] for item in baseline["items"]}
        for item in advice["items"]:
            self.assertEqual(item["evidence"], base_evidence[item["rule"]])
        self.assertEqual(advice["items"][0]["conclusion"], "深蹲最近没有进步。")
        last = self._state()["fitness"]["last_advice"]
        self.assertTrue(last["llm_used"])
        self.assertEqual(last["items"][0]["rule"], "stagnation")

        system, user = self.client.calls[-1]
        self.assertIn("健身建议规则", user)
        self.assertIn("力量", user)
        self.assertIn("2.0t", user)
        self.assertNotIn("肩痛", system + user)
        context = json.loads(user)
        self.assertEqual(len(context["summary"]), 1)
        self.assertEqual(context["summary"][0]["exercise"], "深蹲")
        self.assertEqual(len(context["summary"][0]["weeks"]), 4)

    def test_real_prompt_documents_order_and_texts(self) -> None:
        prompt = (
            Path(__file__).resolve().parents[1] / "prompts" / "fitness-advice.md"
        ).read_text(encoding="utf-8")
        self.assertIn('"order"', prompt)
        self.assertIn('"texts"', prompt)

    def test_model_can_sort_without_rewriting(self) -> None:
        self._weeks()
        self._reply(["stagnation", "frequency"], {})
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertTrue(advice["llm_used"])
        self.assertEqual([item["rule"] for item in advice["items"]], ["stagnation", "frequency"])

    def test_incomplete_order_falls_back_to_script_order_but_keeps_texts(self) -> None:
        self._weeks()
        baseline = self._baseline()
        self._reply(
            ["stagnation"],
            {"stagnation": ("深蹲最近没有进步。", "下次保持重量。")},
        )
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertTrue(advice["llm_used"])
        self.assertEqual([item["rule"] for item in advice["items"]], ["frequency", "stagnation"])
        base_texts = {
            item["rule"]: (item["conclusion"], item["action"]) for item in baseline["items"]
        }
        self.assertEqual(
            (advice["items"][0]["conclusion"], advice["items"][0]["action"]),
            base_texts["frequency"],
        )
        self.assertEqual(advice["items"][1]["conclusion"], "深蹲最近没有进步。")

    def test_unknown_text_key_falls_back_to_templates(self) -> None:
        self._weeks()
        baseline = self._baseline()
        self._reply(
            ["frequency", "stagnation"],
            {"unknown": ("不该出现。", "忽略。")},
        )
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(advice["llm_used"])
        self.assertEqual([item["rule"] for item in advice["items"]], ["frequency", "stagnation"])
        base_texts = {
            item["rule"]: (item["conclusion"], item["action"]) for item in baseline["items"]
        }
        for item in advice["items"]:
            self.assertEqual((item["conclusion"], item["action"]), base_texts[item["rule"]])

    def test_digits_in_model_text_fall_back_entirely(self) -> None:
        self._weeks()
        baseline = self._baseline()
        self._reply(
            ["frequency", "stagnation"],
            {"frequency": ("这周只有 1 次。", "下周补一次。")},
        )
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(advice["llm_used"])
        self.assertEqual(
            [(item["conclusion"], item["action"]) for item in advice["items"]],
            [(item["conclusion"], item["action"]) for item in baseline["items"]],
        )

    def test_model_error_behaves_like_deterministic_advice(self) -> None:
        self._weeks()
        baseline = self._baseline()
        self.client.error = llm.LLMError("network down")
        code, advice = self._advice()
        self.assertEqual(code, 200, advice)
        self.assertFalse(advice["llm_used"])
        self.assertEqual(advice["items"], baseline["items"])
        self.assertEqual(advice["insufficient"], baseline["insufficient"])

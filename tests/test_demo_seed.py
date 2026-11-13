from __future__ import annotations

import contextlib
import io
from datetime import date, timedelta
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpa import cli, fitness, store
from hpa.config import Config


class DemoSeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)
        self.cfg.vault.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _count(self, table: str, where: str = "1=1") -> int:
        conn = store.connect(self.cfg)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])
        finally:
            conn.close()

    def test_seed_has_four_weeks_and_squat_is_two_tonnes_weekly(self) -> None:
        res = fitness.seed_demo(self.cfg)
        self.assertEqual(res["workouts"], 4)
        self.assertEqual(self._count("workouts", "is_demo = 1"), 4)
        self.assertEqual(self._count("weight_logs", "is_demo = 1"), 4)
        conn = store.connect(self.cfg)
        try:
            rows = conn.execute(
                "SELECT w.performed_on, SUM(s.weight_kg * s.reps) AS volume "
                "FROM workouts w JOIN workout_sets s ON s.workout_id = w.id "
                "WHERE w.is_demo = 1 AND s.exercise = 'squat' GROUP BY w.id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["volume"] for row in rows], [2000.0] * 4)
        dates = sorted(row["performed_on"] for row in rows)
        gaps = {
            (date.fromisoformat(b) - date.fromisoformat(a)).days
            for a, b in zip(dates, dates[1:])
        }
        self.assertEqual(gaps, {7})

    def test_seed_is_idempotent(self) -> None:
        fitness.seed_demo(self.cfg)
        fitness.seed_demo(self.cfg)
        self.assertEqual(self._count("workouts", "is_demo = 1"), 4)
        self.assertEqual(self._count("workout_sets"), 4 * (5 + 5 + 3 + 3))
        self.assertEqual(self._count("weight_logs", "is_demo = 1"), 4)

    def test_clear_removes_only_demo_and_keeps_real(self) -> None:
        fitness.seed_demo(self.cfg)
        fitness.record_workout(
            self.cfg,
            {
                "performed_on": fitness.local_today(),
                "started_at": "19:00",
                "exercises": [{"exercise": "deadlift", "sets": [{"weight_kg": 100, "reps": 5}]}],
            },
        )
        fitness.record_weight(
            self.cfg,
            {"logged_on": (date.fromisoformat(fitness.local_today()) - timedelta(days=1)).isoformat(), "weight_kg": 70},
        )
        res = fitness.clear_demo(self.cfg)
        self.assertEqual(res["workouts"], 4)
        self.assertEqual(self._count("workouts", "is_demo = 0"), 1)
        self.assertEqual(self._count("workouts", "is_demo = 1"), 0)
        self.assertEqual(self._count("weight_logs", "is_demo = 1"), 0)
        self.assertEqual(self._count("weight_logs", "is_demo = 0"), 1)

    def test_seed_refuses_when_real_workout_exists(self) -> None:
        fitness.record_workout(
            self.cfg,
            {
                "performed_on": fitness.local_today(),
                "started_at": "19:00",
                "exercises": [{"exercise": "squat", "sets": [{"weight_kg": 60, "reps": 5}]}],
            },
        )
        with self.assertRaisesRegex(ValueError, "真实训练"):
            fitness.seed_demo(self.cfg)
        self.assertEqual(self._count("workouts"), 1)

    def test_seed_does_not_overwrite_existing_profile(self) -> None:
        fitness.record_profile(
            self.cfg,
            {"goal": "增肌", "days_per_week": 4, "equipment": "家里", "injuries": "膝"},
        )
        before = fitness.profile_path(self.cfg).read_text(encoding="utf-8")
        res = fitness.seed_demo(self.cfg)
        self.assertFalse(res["profile_written"])
        self.assertEqual(fitness.profile_path(self.cfg).read_text(encoding="utf-8"), before)
        self.assertEqual(fitness.read_profile(self.cfg)["goal"], "增肌")

    def _cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("hpa.cli.Config.load", return_value=self.cfg):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_seed_and_clear_work(self) -> None:
        code, out, err = self._cli(["demo", "seed"])
        self.assertEqual(code, 0, err)
        self.assertIn("已灌入演示数据", out)
        self.assertEqual(self._count("workouts", "is_demo = 1"), 4)

        code, out, err = self._cli(["demo", "seed", "--clear"])
        self.assertEqual(code, 0, err)
        self.assertIn("已清除演示数据", out)
        self.assertEqual(self._count("workouts", "is_demo = 1"), 0)

    def test_cli_seed_exit_code_when_real_workout_exists(self) -> None:
        fitness.record_workout(
            self.cfg,
            {
                "performed_on": fitness.local_today(),
                "started_at": "19:00",
                "exercises": [{"exercise": "squat", "sets": [{"weight_kg": 60, "reps": 5}]}],
            },
        )
        code, out, err = self._cli(["demo", "seed"])
        self.assertEqual(code, 2)
        self.assertIn("真实训练", err)


if __name__ == "__main__":
    unittest.main()

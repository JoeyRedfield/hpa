"""健身领域的记录与读取。

v1 只支持 7 个固定动作；组按行存，容量由这里计算，UI 不重复实现。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import store
from .config import Config

TZ = ZoneInfo("Asia/Shanghai")
MAX_SETS = 20
MAX_REPS = 100
MAX_WEIGHT_KG = 500
MAX_NOTE = 500
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# id -> (中文名, 部位, 是否自重)
EXERCISES: dict[str, tuple[str, str, bool]] = {
    "bench_press": ("平板卧推", "chest", False),
    "close_grip_bench": ("窄距卧推", "chest", False),
    "pull_up": ("引体向上", "back", True),
    "barbell_curl": ("二头弯举", "biceps", False),
    "hanging_leg_raise": ("悬挂举腿", "core", True),
    "squat": ("深蹲", "legs", False),
    "deadlift": ("硬拉", "legs", False),
}


def catalog() -> list[dict]:
    return [
        {"id": key, "name": name, "group": group, "bodyweight": bodyweight}
        for key, (name, group, bodyweight) in EXERCISES.items()
    ]


def local_today() -> str:
    return datetime.now(TZ).date().isoformat()


def _performed_on(value) -> str:
    text = str(value or "").strip() or local_today()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{text}") from exc
    if parsed.isoformat() > local_today():
        raise ValueError("日期不能晚于今天")
    return parsed.isoformat()


def _started_at(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _TIME_RE.match(text):
        raise ValueError(f"开始时间格式应为 HH:MM：{text}")
    return text


def _reps(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 的次数应为整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} 的次数应为整数")
    reps = int(value)
    if not 1 <= reps <= MAX_REPS:
        raise ValueError(f"{name} 的次数应在 1–{MAX_REPS}")
    return reps


def _weight(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 的重量应为数字")
    weight = float(value)
    if not 0 <= weight <= MAX_WEIGHT_KG:
        raise ValueError(f"{name} 的重量应在 0–{MAX_WEIGHT_KG} kg")
    return weight


def _sets(exercise: str, raw) -> list[tuple[int, float | None, int]]:
    name, _group, bodyweight = EXERCISES[exercise]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{name} 至少记一组")
    if len(raw) > MAX_SETS:
        raise ValueError(f"{name} 一次最多 {MAX_SETS} 组")
    rows: list[tuple[int, float | None, int]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{name} 的组数据格式不对")
        reps = _reps(item.get("reps"), name)
        if bodyweight:
            weight = None
        else:
            if item.get("weight_kg") is None:
                raise ValueError(f"{name} 需要填重量")
            weight = _weight(item["weight_kg"], name)
        rows.append((len(rows) + 1, weight, reps))
    return rows


def record_workout(cfg: Config, payload: dict) -> dict:
    performed_on = _performed_on(payload.get("performed_on"))
    started_at = _started_at(payload.get("started_at"))
    note = str(payload.get("note") or "").strip()
    if len(note) > MAX_NOTE:
        raise ValueError(f"备注最多 {MAX_NOTE} 字")
    raw_exercises = payload.get("exercises")
    if not isinstance(raw_exercises, list) or not raw_exercises:
        raise ValueError("至少记一个动作")

    normalized: list[tuple[str, list[tuple[int, float | None, int]]]] = []
    for item in raw_exercises:
        if not isinstance(item, dict):
            raise ValueError("动作数据格式不对")
        exercise = str(item.get("exercise") or "")
        if exercise not in EXERCISES:
            raise ValueError(f"未知动作：{exercise}")
        normalized.append((exercise, _sets(exercise, item.get("sets"))))

    conn = store.connect(cfg)
    try:
        cur = conn.execute(
            "INSERT INTO workouts(performed_on, started_at, note, created_at) VALUES (?, ?, ?, ?)",
            (performed_on, started_at, note, store.utcnow()),
        )
        workout_id = cur.lastrowid
        for exercise, rows in normalized:
            conn.executemany(
                "INSERT INTO workout_sets(workout_id, exercise, set_index, weight_kg, reps) "
                "VALUES (?, ?, ?, ?, ?)",
                [(workout_id, exercise, index, weight, reps) for index, weight, reps in rows],
            )
        conn.commit()
    finally:
        conn.close()
    return {"status": "recorded", "id": workout_id}


def delete_workout(cfg: Config, workout_id: int, confirm: bool) -> dict:
    if not confirm:
        raise ValueError("删除训练需要确认（confirm: true）")
    conn = store.connect(cfg)
    try:
        if conn.execute("SELECT id FROM workouts WHERE id = ?", (workout_id,)).fetchone() is None:
            raise ValueError(f"训练 {workout_id} 不存在")
        conn.execute("DELETE FROM workout_sets WHERE workout_id = ?", (workout_id,))
        conn.execute("DELETE FROM workouts WHERE id = ?", (workout_id,))
        conn.commit()
    finally:
        conn.close()
    return {"status": "deleted", "id": workout_id}


def _recent_workouts(conn, limit: int = 20) -> list[dict]:
    workouts = conn.execute(
        "SELECT id, performed_on, started_at, note, is_demo, created_at FROM workouts "
        "ORDER BY performed_on DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result: list[dict] = []
    for workout in workouts:
        rows = conn.execute(
            "SELECT exercise, set_index, weight_kg, reps FROM workout_sets "
            "WHERE workout_id = ? ORDER BY set_index",
            (workout["id"],),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["exercise"], []).append(
                {"set_index": row["set_index"], "weight_kg": row["weight_kg"], "reps": row["reps"]}
            )
        exercises = []
        for exercise, (_name, _group, bodyweight) in EXERCISES.items():
            if exercise not in grouped:
                continue
            sets = grouped[exercise]
            if bodyweight:
                volume = sum(item["reps"] for item in sets)
                volume_kind = "reps"
            else:
                volume = sum((item["weight_kg"] or 0) * item["reps"] for item in sets)
                volume_kind = "kg"
            exercises.append(
                {
                    "exercise": exercise,
                    "name": EXERCISES[exercise][0],
                    "sets": sets,
                    "volume": round(volume, 2),
                    "volume_kind": volume_kind,
                }
            )
        result.append(
            {
                "id": workout["id"],
                "performed_on": workout["performed_on"],
                "started_at": workout["started_at"],
                "note": workout["note"],
                "is_demo": bool(workout["is_demo"]),
                "created_at": workout["created_at"],
                "exercises": exercises,
            }
        )
    return result


def state(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        recent = _recent_workouts(conn)
    finally:
        conn.close()
    return {"exercises": catalog(), "recent_workouts": recent}

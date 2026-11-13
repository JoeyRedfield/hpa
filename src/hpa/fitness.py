"""健身领域的记录与读取。

v1 只支持 7 个固定动作；组按行存，容量由这里计算，UI 不重复实现。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import llm, store
from .config import Config

TZ = ZoneInfo("Asia/Shanghai")
MAX_SETS = 20
MAX_REPS = 100
MAX_WEIGHT_KG = 500
MIN_BODY_WEIGHT_KG = 20
MAX_BODY_WEIGHT_KG = 300
MAX_NOTE = 500
PROFILE_KEYS = ("目标", "每周可练", "器械", "伤病")
GOALS = ("力量", "增肌", "减脂", "维持", "未设定")
MAX_PROFILE_TEXT = 80
_PROFILE_LINE_RE = re.compile(r"^(" + "|".join(PROFILE_KEYS) + r")\s*[:：]\s*(.*)$")
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


def _body_weight(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("体重应为数字")
    weight = float(value)
    if not MIN_BODY_WEIGHT_KG <= weight <= MAX_BODY_WEIGHT_KG:
        raise ValueError(f"体重应在 {MIN_BODY_WEIGHT_KG}–{MAX_BODY_WEIGHT_KG} kg")
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


def _exercise_payload(exercise: str, raw_sets: list[dict]) -> dict:
    name, _group, bodyweight = EXERCISES[exercise]
    rows = _sets(exercise, raw_sets)
    return {
        "exercise": exercise,
        "name": name,
        "bodyweight": bodyweight,
        "sets": [{"weight_kg": weight, "reps": reps} for _index, weight, reps in rows],
    }


# 快捷输入的动作别名；同一位置取最长匹配，避免「卧推」抢走「平板卧推」。
_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("close_grip_bench", ("窄距卧推", "窄距", "窄推")),
    ("bench_press", ("平板卧推", "卧推")),
    ("hanging_leg_raise", ("悬挂举腿", "悬举腿")),
    ("barbell_curl", ("二头弯举", "弯举")),
    ("pull_up", ("引体向上", "引体")),
    ("deadlift", ("硬拉",)),
    ("squat", ("深蹲",)),
)
_WEIGHT_UNIT_RE = r"(\d+(?:\.\d+)?)\s*(?:kg|KG|公斤|千克)"
_SETS_REPS_RE = r"(\d+)\s*组\s*[x×*]?\s*(\d+)\s*次"
_REPS_SETS_RE = r"(\d+)\s*次\s*[x×*]?\s*(\d+)\s*组"
_PAIR_RE = r"(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)"
_TRIPLE_RE = (
    r"(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+(?:\.\d+)?)"
)


def _split_exercise(text: str) -> tuple[str, str] | None:
    best: tuple[int, int, str, str] | None = None
    for key, aliases in _ALIASES:
        for alias in aliases:
            index = text.find(alias)
            if index < 0:
                continue
            candidate = (index, -len(alias), key, alias)
            if best is None or candidate < best:
                best = candidate
    if best is None:
        return None
    index, _neg_len, key, alias = best
    return key, (text[:index] + " " + text[index + len(alias) :]).strip()


def _consume(pattern: str, text: str) -> tuple[re.Match[str] | None, str]:
    match = re.search(pattern, text)
    if match is None:
        return None, text
    return match, (text[: match.start()] + " " + text[match.end() :]).strip()


def parse_quick(text: str) -> dict | None:
    """本地解析一句话；没命中返回 None 交给模型，命中但数值非法抛 ValueError。不写库。"""
    raw = (text or "").strip()
    if not raw:
        return None
    split = _split_exercise(raw)
    if split is None:
        return None
    key, rest = split
    name, _group, bodyweight = EXERCISES[key]

    weight: float | None = None
    match, rest = _consume(_WEIGHT_UNIT_RE, rest)
    if match:
        weight = float(match.group(1))

    sets: int | None = None
    reps: int | None = None
    # 「80×5×5」是页面教的默认写法：重量×次数×组数。
    if weight is None and not bodyweight:
        match, rest = _consume(_TRIPLE_RE, rest)
        if match:
            raw_weight = float(match.group(1))
            raw_reps = float(match.group(2))
            raw_sets = float(match.group(3))
            if not raw_reps.is_integer() or not raw_sets.is_integer():
                raise ValueError(f"{name} 的次数和组数必须是整数")
            weight, reps, sets = raw_weight, int(raw_reps), int(raw_sets)
    if sets is None:
        match, rest = _consume(_SETS_REPS_RE, rest)
        if match:
            sets, reps = int(match.group(1)), int(match.group(2))
        else:
            match, rest = _consume(_REPS_SETS_RE, rest)
            if match:
                reps, sets = int(match.group(1)), int(match.group(2))
            else:
                match, rest = _consume(_PAIR_RE, rest)
                if match:
                    first = float(match.group(1))
                    second = float(match.group(2))
                    if not first.is_integer() or not second.is_integer():
                        raise ValueError(f"{name} 的次数和组数必须是整数")
                    reps, sets = int(first), int(second)
    if sets is None or reps is None:
        return None

    if weight is None and not bodyweight:
        match, rest = _consume(r"(\d+(?:\.\d+)?)", rest)
        if not match:
            return None
        weight = float(match.group(1))

    if not 1 <= sets <= MAX_SETS:
        raise ValueError(f"{name} 的组数应在 1–{MAX_SETS}")
    exercise = _exercise_payload(
        key, [{"weight_kg": weight, "reps": reps} for _ in range(sets)]
    )

    # 一句话里出现第二个动作时不要静默丢掉，交给模型整体解析。
    if _split_exercise(rest) is not None:
        return None

    return {"parser": "rules", "draft": {"exercises": [exercise]}, "uncertain": []}


def parse_with_llm(cfg: Config, text: str, client: llm.LLM) -> dict:
    """模型兜底解析；结果按与服务端相同的规则校验。失败由调用方降级。"""
    system = llm.load_prompt(cfg, "fitness-parse.md")
    data = llm.parse_json(client.complete(system, text))
    raw_exercises = data.get("exercises")
    if not isinstance(raw_exercises, list) or not raw_exercises:
        raise ValueError("模型没有返回动作")
    exercises = []
    for item in raw_exercises:
        if not isinstance(item, dict) or item.get("exercise") not in EXERCISES:
            raise ValueError("模型返回了未知动作")
        key = item["exercise"]
        exercises.append(_exercise_payload(key, item.get("sets")))
    uncertain = data.get("uncertain") or []
    if not isinstance(uncertain, list):
        raise ValueError("模型返回的 uncertain 不是列表")
    return {
        "parser": "llm",
        "draft": {"exercises": exercises},
        "uncertain": [str(item)[:60] for item in uncertain if str(item).strip()][:10],
    }


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


def record_weight(cfg: Config, payload: dict) -> dict:
    logged_on = _performed_on(payload.get("logged_on"))
    weight = _body_weight(payload.get("weight_kg"))
    conn = store.connect(cfg)
    try:
        conn.execute(
            "INSERT INTO weight_logs(logged_on, weight_kg, is_demo) VALUES (?, ?, 0) "
            "ON CONFLICT(logged_on) DO UPDATE SET "
            "weight_kg = excluded.weight_kg, is_demo = 0",
            (logged_on, weight),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "recorded", "logged_on": logged_on, "weight_kg": weight}


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


def profile_path(cfg: Config) -> Path:
    return cfg.project_root / "memory" / "training-profile.md"


def _parse_profile(text: str) -> dict:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        match = _PROFILE_LINE_RE.match(line.strip())
        if match and match.group(1) not in raw:
            raw[match.group(1)] = match.group(2).strip()

    goal = raw.get("目标", "")
    if goal not in GOALS:
        goal = ""
    days_text = raw.get("每周可练", "")
    try:
        days = int(days_text)
    except ValueError:
        days = None
    if days is not None and not 1 <= days <= 7:
        days = None
    equipment = raw.get("器械", "")
    injuries = raw.get("伤病", "")
    if len(equipment) > MAX_PROFILE_TEXT:
        equipment = ""
    if len(injuries) > MAX_PROFILE_TEXT:
        injuries = ""

    missing = []
    if not goal:
        missing.append("目标")
    if days is None:
        missing.append("每周可练")
    if not equipment:
        missing.append("器械")
    if not injuries:
        missing.append("伤病")
    return {
        "goal": goal,
        "days_per_week": days,
        "equipment": equipment,
        "injuries": injuries,
        "complete": not missing,
        "missing": missing,
    }


def read_profile(cfg: Config) -> dict:
    try:
        text = profile_path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return _parse_profile(text)


def _profile_values(payload: dict) -> dict:
    goal = str(payload.get("goal") or "").strip()
    if goal not in GOALS:
        raise ValueError(f"目标应为：{' / '.join(GOALS)}")
    days_raw = payload.get("days_per_week")
    if isinstance(days_raw, bool) or not isinstance(days_raw, (int, float)):
        raise ValueError("每周可练应为 1–7 的整数")
    if isinstance(days_raw, float) and not days_raw.is_integer():
        raise ValueError("每周可练应为 1–7 的整数")
    days = int(days_raw)
    if not 1 <= days <= 7:
        raise ValueError("每周可练应为 1–7 的整数")
    values = {"目标": goal, "每周可练": str(days)}
    for key, field in (("器械", "equipment"), ("伤病", "injuries")):
        value = str(payload.get(field) or "").strip()
        if not value:
            raise ValueError(f"{key}不能为空")
        if "\n" in value or "\r" in value:
            raise ValueError(f"{key}不能换行")
        if len(value) > MAX_PROFILE_TEXT:
            raise ValueError(f"{key}最多 {MAX_PROFILE_TEXT} 字")
        values[key] = value
    return values


def _render_profile(values: dict, existing: str) -> str:
    lines = existing.splitlines()
    index = 0
    seen = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if seen < len(PROFILE_KEYS) and _PROFILE_LINE_RE.match(stripped):
            seen += 1
            index += 1
            continue
        break
    body = lines[index:]
    while body and not body[-1].strip():
        body.pop()
    header = "\n".join(f"{key}：{values[key]}" for key in PROFILE_KEYS)
    if body:
        return header + "\n\n" + "\n".join(body) + "\n"
    return header + "\n"


def record_profile(cfg: Config, payload: dict) -> dict:
    values = _profile_values(payload)
    path = profile_path(cfg)
    try:
        existing = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        existing = ""
    rendered = _render_profile(values, existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"status": "recorded", "profile": _parse_profile(rendered)}


def _recent_weights(conn, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT logged_on, weight_kg, is_demo FROM weight_logs "
        "ORDER BY logged_on DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "logged_on": row["logged_on"],
            "weight_kg": row["weight_kg"],
            "is_demo": bool(row["is_demo"]),
        }
        for row in rows
    ]


def state(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        recent = _recent_workouts(conn)
        weights = _recent_weights(conn)
    finally:
        conn.close()
    return {
        "exercises": catalog(),
        "recent_workouts": recent,
        "weights": weights,
        "profile": read_profile(cfg),
    }

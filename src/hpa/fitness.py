"""健身领域的记录与读取。

v1 只支持 7 个固定动作；组按行存，容量由这里计算，UI 不重复实现。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
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
MAX_REST_SECONDS = 600
MAX_SEGMENTS = 6
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
    "lateral_raise": ("侧平举", "shoulders", False),
}

DEMO_PROFILE: dict[str, object] = {
    "goal": "力量",
    "days_per_week": 3,
    "equipment": "健身房",
    "injuries": "无伤",
}
# 每周一次，四个动作按验收数据；自重动作 weight 为 None
DEMO_EXERCISES: tuple[tuple[str, float | None, int, int], ...] = (
    ("squat", 80.0, 5, 5),
    ("bench_press", 60.0, 5, 5),
    ("pull_up", None, 8, 3),
    ("hanging_leg_raise", None, 12, 3),
)
DEMO_WEEKS = 4
_DEMO_PROFILE_MARKER = "<!-- 演示画像：hpa demo seed 生成，--clear 会删除 -->"


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


def _rest_seconds(value, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 的组间歇应为整数秒")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} 的组间歇应为整数秒")
    seconds = int(value)
    if not 0 <= seconds <= MAX_REST_SECONDS:
        raise ValueError(f"{name} 的组间歇应在 0–{MAX_REST_SECONDS} 秒")
    return seconds


def _rest_pair(item: dict, name: str) -> tuple[int | None, int | None]:
    rest = _rest_seconds(item.get("rest_seconds"), name)
    rest_max = _rest_seconds(item.get("rest_seconds_max"), name)
    if rest_max is not None and rest is None:
        raise ValueError(f"{name} 的组间歇要先填最短时间")
    if rest is not None and rest_max is not None and rest_max < rest:
        raise ValueError(f"{name} 的组间歇最长时间不能小于最短时间")
    return rest, rest_max


def _sets(
    exercise: str,
    raw,
    rest_seconds: int | None = None,
    rest_seconds_max: int | None = None,
    warmup: bool = False,
) -> list[dict]:
    name, _group, bodyweight = EXERCISES[exercise]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{name} 至少记一组")
    if len(raw) > MAX_SETS:
        raise ValueError(f"{name} 一次最多 {MAX_SETS} 组")
    rows: list[dict] = []
    for set_index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{name} 的组数据格式不对")
        set_warmup = item.get("warmup", warmup) is True
        segments = item.get("segments")
        if segments is None:
            segments = [item]
        if not isinstance(segments, list) or not segments:
            raise ValueError(f"{name} 的组内至少有一段")
        if len(segments) > MAX_SEGMENTS:
            raise ValueError(f"{name} 一组最多 {MAX_SEGMENTS} 段")
        for drop_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ValueError(f"{name} 的递减段格式不对")
            reps = _reps(segment.get("reps"), name)
            if bodyweight:
                weight = None
            else:
                if segment.get("weight_kg") is None:
                    raise ValueError(f"{name} 需要填重量")
                weight = _weight(segment["weight_kg"], name)
            rows.append(
                {
                    "set_index": set_index,
                    "drop_index": drop_index,
                    "weight_kg": weight,
                    "reps": reps,
                    "rest_seconds": rest_seconds,
                    "rest_seconds_max": rest_seconds_max,
                    "warmup": set_warmup,
                }
            )
    return rows


def _exercise_payload(exercise: str, raw_sets: list[dict]) -> dict:
    name, _group, bodyweight = EXERCISES[exercise]
    rows = _sets(exercise, raw_sets)
    return {
        "exercise": exercise,
        "name": name,
        "bodyweight": bodyweight,
        "sets": [{"weight_kg": row["weight_kg"], "reps": row["reps"]} for row in rows],
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
    ("lateral_raise", ("侧平举", "侧平", "侧举")),
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

    normalized: list[tuple[str, list[dict]]] = []
    set_offsets: dict[str, int] = {}
    for item in raw_exercises:
        if not isinstance(item, dict):
            raise ValueError("动作数据格式不对")
        exercise = str(item.get("exercise") or "")
        if exercise not in EXERCISES:
            raise ValueError(f"未知动作：{exercise}")
        name = EXERCISES[exercise][0]
        rest, rest_max = _rest_pair(item, name)
        rows = _sets(
            exercise,
            item.get("sets"),
            rest_seconds=rest,
            rest_seconds_max=rest_max,
            warmup=item.get("warmup") is True,
        )
        offset = set_offsets.get(exercise, 0)
        for row in rows:
            row["set_index"] += offset
        set_offsets[exercise] = max(row["set_index"] for row in rows)
        normalized.append((exercise, rows))

    conn = store.connect(cfg)
    try:
        cur = conn.execute(
            "INSERT INTO workouts(performed_on, started_at, note, created_at) VALUES (?, ?, ?, ?)",
            (performed_on, started_at, note, store.utcnow()),
        )
        workout_id = cur.lastrowid
        for exercise, rows in normalized:
            conn.executemany(
                "INSERT INTO workout_sets(workout_id, exercise, set_index, drop_index, "
                "weight_kg, reps, rest_seconds, rest_seconds_max, warmup) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        workout_id,
                        exercise,
                        row["set_index"],
                        row["drop_index"],
                        row["weight_kg"],
                        row["reps"],
                        row["rest_seconds"],
                        row["rest_seconds_max"],
                        int(row["warmup"]),
                    )
                    for row in rows
                ],
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


def _has_demo(conn) -> bool:
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM workouts WHERE is_demo = 1) "
        "OR EXISTS(SELECT 1 FROM weight_logs WHERE is_demo = 1)"
    ).fetchone()
    return bool(row[0])


def _demo_counts(conn) -> tuple[int, int]:
    workouts = conn.execute(
        "SELECT COUNT(*) FROM workouts WHERE is_demo = 1"
    ).fetchone()[0]
    weights = conn.execute(
        "SELECT COUNT(*) FROM weight_logs WHERE is_demo = 1"
    ).fetchone()[0]
    return int(workouts), int(weights)


def _delete_demo(conn) -> None:
    conn.execute(
        "DELETE FROM workout_sets WHERE workout_id IN "
        "(SELECT id FROM workouts WHERE is_demo = 1)"
    )
    conn.execute("DELETE FROM workouts WHERE is_demo = 1")
    conn.execute("DELETE FROM weight_logs WHERE is_demo = 1")


def clear_demo(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        workouts, weights = _demo_counts(conn)
        _delete_demo(conn)
        conn.commit()
    finally:
        conn.close()
    profile_removed = False
    path = profile_path(cfg)
    try:
        if path.exists() and _DEMO_PROFILE_MARKER in path.read_text(encoding="utf-8"):
            path.unlink()
            profile_removed = True
    except OSError:
        pass
    return {
        "status": "cleared",
        "workouts": workouts,
        "weights": weights,
        "profile_removed": profile_removed,
    }


def seed_demo(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        real = conn.execute(
            "SELECT COUNT(*) FROM workouts WHERE is_demo = 0"
        ).fetchone()[0]
        if real:
            raise ValueError(
                "已有真实训练记录，演示种子会污染统计；请换独立数据目录再灌。"
            )
        _delete_demo(conn)
        today = date.fromisoformat(local_today())
        created = store.utcnow()
        for week in range(DEMO_WEEKS - 1, -1, -1):
            performed_on = (today - timedelta(days=week * 7)).isoformat()
            cur = conn.execute(
                "INSERT INTO workouts(performed_on, started_at, note, is_demo, created_at) "
                "VALUES (?, '18:00', '', 1, ?)",
                (performed_on, created),
            )
            workout_id = cur.lastrowid
            for exercise, weight, reps, sets in DEMO_EXERCISES:
                conn.executemany(
                    "INSERT INTO workout_sets(workout_id, exercise, set_index, weight_kg, reps) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(workout_id, exercise, index, weight, reps) for index in range(1, sets + 1)],
                )
            # 真实体重若占用了同一天，保留真实值；演示体重可能少于 4 条。
            conn.execute(
                "INSERT INTO weight_logs(logged_on, weight_kg, is_demo) VALUES (?, 75.0, 1) "
                "ON CONFLICT(logged_on) DO NOTHING",
                (performed_on,),
            )
        conn.commit()
        workouts, weights = _demo_counts(conn)
    finally:
        conn.close()
    profile_written = False
    if not profile_path(cfg).exists():
        try:
            record_profile(cfg, DEMO_PROFILE)
        except OSError as exc:
            raise ValueError(
                f"演示训练已写入，但训练画像写入失败：{exc}；可修好目录后重跑 seed。"
            ) from exc
        with profile_path(cfg).open("a", encoding="utf-8") as handle:
            handle.write(f"\n{_DEMO_PROFILE_MARKER}\n")
        profile_written = True
    return {
        "status": "seeded",
        "workouts": workouts,
        "weights": weights,
        "profile_written": profile_written,
    }


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
            "SELECT exercise, set_index, drop_index, weight_kg, reps, "
            "rest_seconds, rest_seconds_max, warmup FROM workout_sets "
            "WHERE workout_id = ? ORDER BY set_index, drop_index",
            (workout["id"],),
        ).fetchall()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["exercise"], []).append(
                {
                    "set_index": row["set_index"],
                    "drop_index": row["drop_index"],
                    "weight_kg": row["weight_kg"],
                    "reps": row["reps"],
                    "rest_seconds": row["rest_seconds"],
                    "rest_seconds_max": row["rest_seconds_max"],
                    "warmup": bool(row["warmup"]),
                }
            )
        exercises = []
        for exercise, (_name, _group, bodyweight) in EXERCISES.items():
            if exercise not in grouped:
                continue
            sets = grouped[exercise]
            working = [item for item in sets if not item["warmup"]]
            if bodyweight:
                volume = sum(item["reps"] for item in working)
                volume_kind = "reps"
            else:
                volume = sum((item["weight_kg"] or 0) * item["reps"] for item in working)
                volume_kind = "kg"
            exercises.append(
                {
                    "exercise": exercise,
                    "name": EXERCISES[exercise][0],
                    "sets": sets,
                    "set_count": len({item["set_index"] for item in sets}),
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


PART_NAMES = {"chest": "胸", "back": "背", "biceps": "二头", "legs": "腿", "core": "核心", "shoulders": "肩"}
_PART_ORDER = tuple(dict.fromkeys(spec[1] for spec in EXERCISES.values()))
_RULE_ORDER = {"frequency": 0, "volume": 1, "stagnation": 2}


def _buckets(today: date) -> list[dict]:
    labels = ("3 周前", "2 周前", "上周", "本周至今")
    return [
        {
            "start": today - timedelta(days=27 - 7 * index),
            "end": today - timedelta(days=21 - 7 * index),
            "label": labels[index],
        }
        for index in range(4)
    ]


def _bucket_index(day: date, today: date) -> int | None:
    age = (today - day).days
    if age < 0 or age > 27:
        return None
    return 3 - age // 7


def _session_datetime(workout: dict) -> datetime:
    started = workout.get("started_at") or "00:00"
    try:
        hour, minute = (int(part) for part in started.split(":"))
    except (TypeError, ValueError):
        hour, minute = 0, 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        hour, minute = 0, 0
    return datetime(
        workout["day"].year, workout["day"].month, workout["day"].day, hour, minute, tzinfo=TZ
    )


def _short(day: date) -> str:
    return day.strftime("%m-%d")


def _hours_text(hours: float) -> str:
    return f"{int(hours * 10) / 10:g}"


def _volume_text(value: float, bodyweight: bool) -> str:
    if bodyweight:
        return f"{value:.0f} 次"
    if value >= 1000:
        return f"{value / 1000:.1f}t"
    return f"{value:.0f}kg"


def _best_text(value: float, bodyweight: bool) -> str:
    return f"{value:.0f} 次" if bodyweight else f"{value:.1f}kg"


def _series(name: str, values: list[float], bodyweight: bool, span: str = "") -> str:
    return f"{name} 周容量{span}：" + " → ".join(
        _volume_text(value, bodyweight) for value in values
    )


def _span(buckets: list[dict]) -> str:
    return f"（{_short(buckets[0]['start'])}–{_short(buckets[-1]['end'])}，按周）"


def _window_workouts(conn, today: date) -> list[dict]:
    start = today - timedelta(days=27)
    rows = conn.execute(
        "SELECT id, performed_on, started_at FROM workouts "
        "WHERE performed_on BETWEEN ? AND ? ORDER BY performed_on, id",
        (start.isoformat(), today.isoformat()),
    ).fetchall()
    workouts: list[dict] = []
    for row in rows:
        try:
            day = date.fromisoformat(row["performed_on"])
        except ValueError:
            continue
        sets = conn.execute(
            "SELECT exercise, weight_kg, reps FROM workout_sets "
            "WHERE workout_id = ? AND warmup = 0 ORDER BY set_index, drop_index",
            (row["id"],),
        ).fetchall()
        grouped: dict[str, list[tuple[float | None, int]]] = {}
        for item in sets:
            if item["exercise"] in EXERCISES:
                grouped.setdefault(item["exercise"], []).append(
                    (item["weight_kg"], item["reps"])
                )
        if grouped:
            workouts.append(
                {"id": row["id"], "day": day, "started_at": row["started_at"], "sets": grouped}
            )
    return workouts


def _stats(workouts: list[dict], today: date) -> dict[str, dict]:
    stats = {
        key: {"volumes": [0.0] * 4, "bests": [0.0] * 4, "sessions": [0] * 4, "ids": [[], [], [], []]}
        for key in EXERCISES
    }
    for workout in workouts:
        index = _bucket_index(workout["day"], today)
        if index is None:
            continue
        for key, rows in workout["sets"].items():
            _name, _group, bodyweight = EXERCISES[key]
            volume = sum(
                (reps if bodyweight else float(weight or 0) * reps) for weight, reps in rows
            )
            best = max(
                (reps if bodyweight else float(weight or 0) * (1 + reps / 30))
                for weight, reps in rows
            )
            entry = stats[key]
            entry["volumes"][index] += volume
            entry["bests"][index] = max(entry["bests"][index], best)
            entry["sessions"][index] += 1
            entry["ids"][index].append(workout["id"])
    return stats


def _frequency_candidate(
    workouts: list[dict], training_days: list[date], profile: dict, buckets: list[dict]
) -> tuple[dict | None, str | None]:
    part_sessions: dict[str, list[tuple[datetime, date, int]]] = {}
    for workout in workouts:
        moment = _session_datetime(workout)
        present = {EXERCISES[key][1] for key in workout["sets"]}
        for part in _PART_ORDER:
            if part in present:
                part_sessions.setdefault(part, []).append(
                    (moment, workout["day"], workout["id"])
                )

    best: tuple[tuple[float, float, int], float, str, tuple, tuple] | None = None
    for part, sessions in part_sessions.items():
        sessions.sort(key=lambda item: item[0])
        for left, right in zip(sessions, sessions[1:]):
            hours = (right[0] - left[0]).total_seconds() / 3600
            if 0 <= hours < 48:
                rank = (-right[0].timestamp(), hours, _PART_ORDER.index(part))
                if best is None or rank < best[0]:
                    best = (rank, hours, part, left, right)

    if best is not None:
        _rank, hours, part, left, right = best
        part_cn = PART_NAMES[part]
        return (
            {
                "rule": "frequency",
                "conclusion": f"{part_cn}的两次训练间隔太近。",
                "evidence": (
                    f"{part_cn}：{_short(left[1])} 与 {_short(right[1])} "
                    f"相隔 {_hours_text(hours)} 小时（< 48h）。"
                ),
                "action": f"把{part_cn}的两次训练隔开至少两天。",
                "workout_ids": [left[2], right[2]],
                "_score": 90,
            },
            None,
        )

    target = profile.get("days_per_week")
    if not target:
        return None, "训练画像缺「每周可练」，暂时无法判断频率是否合适。"
    current = buckets[3]
    ids = [
        workout["id"]
        for workout in workouts
        if current["start"] <= workout["day"] <= current["end"]
    ]
    count = sum(1 for day in training_days if current["start"] <= day <= current["end"])
    if count == 0:
        return None, "本周还没有训练记录，先恢复一次再评估频率。"
    if count < target:
        return (
            {
                "rule": "frequency",
                "conclusion": "最近一周训练次数低于画像目标。",
                "evidence": (
                    f"本周至今（{_short(current['start'])}–{_short(current['end'])}）"
                    f" {count} 次，画像目标 {target} 次/周。"
                ),
                "action": "下周先补到目标次数，优先安排一次全身或下肢。",
                "workout_ids": ids,
                "_score": 70 + min(20, (target - count) * 10),
            },
            None,
        )
    if count > target + 1:
        return (
            {
                "rule": "frequency",
                "conclusion": "最近一周训练次数高于画像目标。",
                "evidence": (
                    f"本周至今（{_short(current['start'])}–{_short(current['end'])}）"
                    f" {count} 次，画像目标 {target} 次/周。"
                ),
                "action": "留出至少一天完全休息，避免连续高强度。",
                "workout_ids": ids,
                "_score": 60,
            },
            None,
        )
    return None, None


def _volume_candidate(stats: dict[str, dict], buckets: list[dict]) -> dict | None:
    candidates: list[dict] = []
    for key, entry in stats.items():
        name, _group, bodyweight = EXERCISES[key]
        volumes = entry["volumes"]
        v1, v2, v3 = volumes[1], volumes[2], volumes[3]
        ids = [item for bucket in entry["ids"] for item in bucket]
        if v1 > 0 and v2 > 0 and v3 > 0 and v2 < v1 * 0.9 and v3 < v2 * 0.9:
            reason = "连续两周下降 >10%"
            score = 80 + min(15, (v1 - v3) / v1 * 50)
        else:
            spike = None
            for before, after in ((v1, v2), (v2, v3)):
                if before > 0 and after > before * 1.3:
                    ratio = after / before
                    if spike is None or ratio > spike:
                        spike = ratio
            if spike is None:
                continue
            reason = "单周上升 >30%"
            score = 70 + min(15, (spike - 1) * 30)
        candidates.append(
            {
                "rule": "volume",
                "exercise": key,
                "conclusion": (
                    f"{name}的周容量在下降。" if reason.startswith("连续") else f"{name}的周容量单周涨得太多。"
                ),
                "evidence": (
                    f"{_series(name, volumes, bodyweight, _span(buckets))}（{reason}）。"
                ),
                "action": (
                    f"下次{name}先减一组，把动作质量拉回来；容量回到下降前再谈加重。"
                    if reason.startswith("连续")
                    else "下周先保持同样重量，别继续加量，观察恢复。"
                ),
                "workout_ids": ids,
                "_score": score,
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["_score"], len(item["workout_ids"])))


def _stagnation_candidate(
    stats: dict[str, dict], skip: set[str], buckets: list[dict], weekly_days: int | None
) -> dict | None:
    candidates: list[dict] = []
    for key, entry in stats.items():
        if key in skip:
            continue
        sessions = entry["sessions"]
        if sum(sessions[1:4]) < 3:
            continue
        present = [index for index in (1, 2, 3) if sessions[index] > 0]
        if len(present) < 2:
            continue
        name, _group, bodyweight = EXERCISES[key]
        bests = entry["bests"]
        volumes = entry["volumes"]
        baseline = present[0]
        later = present[1:]
        if max(bests[index] for index in later) > bests[baseline]:
            continue
        if max(volumes[index] for index in later) > volumes[baseline] * 1.05:
            continue
        ids = [item for index in present for item in entry["ids"][index]]
        candidates.append(
            {
                "rule": "stagnation",
                "exercise": key,
                "conclusion": f"{name}最近三周没有明显进步。",
                "evidence": (
                    f"{name} 最近 3 周最好一组 {_best_text(max(bests[1:4]), bodyweight)} 没有提升；"
                    f"{_series(name, volumes[1:], bodyweight, _span(buckets[1:]))}。"
                ),
                "action": (
                    f"下次{name}试着加重量（2.5kg）或把最后一组多做一次；连续两周仍无变化再考虑减量。"
                    if weekly_days is not None and weekly_days > 2
                    else (
                        f"下次{name}保持重量，把最后一组多做一次；一周只练 {weekly_days} 次，先稳住恢复。"
                        if weekly_days
                        else f"下次{name}保持重量，把最后一组多做一次；先稳住动作与恢复。"
                    )
                ),
                "workout_ids": ids,
                "_score": 60 + min(20, sum(sessions) * 5),
                "total": sum(volumes),
                "bodyweight": bodyweight,
            }
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (item["_score"], not item["bodyweight"], item["total"]),
    )


def _build_advice(workouts: list[dict], profile: dict, today: date) -> dict:
    buckets = _buckets(today)
    training_days = sorted({workout["day"] for workout in workouts})
    stats = _stats(workouts, today)
    items: list[dict] = []
    insufficient: list[str] = []

    if len(training_days) < 2:
        insufficient.append("至少记录 2 天训练，才能给出频率或趋势建议。")
    else:
        frequency, reason = _frequency_candidate(workouts, training_days, profile, buckets)
        if reason:
            insufficient.append(reason)
        if frequency:
            items.append(frequency)
        volume = _volume_candidate(stats, buckets)
        skip: set[str] = set()
        if volume:
            items.append(volume)
            skip.add(volume["exercise"])
        stagnation = _stagnation_candidate(stats, skip, buckets, profile.get("days_per_week"))
        if stagnation:
            items.append(stagnation)

    for key, entry in stats.items():
        total = sum(entry["sessions"])
        if 0 < total < 3:
            insufficient.append(
                f"{EXERCISES[key][0]} 只练了 {total} 次，再记 {3 - total} 次才能判断是否停滞。"
            )
    if profile["missing"]:
        insufficient.append("训练画像还缺：" + "、".join(profile["missing"]) + "。")
    if not items and len(training_days) >= 2:
        insufficient.append("目前没有发现需要调整的地方；保持记录，过两周再看。")

    items.sort(key=lambda item: (-item["_score"], _RULE_ORDER[item["rule"]]))
    for item in items:
        item.pop("_score", None)
        item.pop("exercise", None)
        item.pop("total", None)
        item.pop("bodyweight", None)

    seen: set[str] = set()
    unique: list[str] = []
    for note in insufficient:
        if note not in seen:
            seen.add(note)
            unique.append(note)
    return {
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "llm_used": False,
        "items": items[:3],
        "insufficient": unique,
    }


def _last_advice(conn) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM advice_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def growth_context(cfg: Config) -> dict:
    """给成长循环的健身上下文：画像 + 4 周摘要 + 上次建议，不含备注。"""
    today = date.fromisoformat(local_today())
    conn = store.connect(cfg)
    try:
        workouts = _window_workouts(conn, today)
        last_advice = _last_advice(conn)
    finally:
        conn.close()
    return {
        "profile": read_profile(cfg),
        "summary": _training_summary(workouts, today),
        "last_advice": last_advice,
    }


def _advice_rules(cfg: Config) -> str:
    path = cfg.project_root / "rules" / "fitness-advice.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _training_summary(workouts: list[dict], today: date) -> list[dict]:
    buckets = _buckets(today)
    stats = _stats(workouts, today)
    summary = []
    for key, entry in stats.items():
        if sum(entry["sessions"]) == 0:
            continue
        name, _group, bodyweight = EXERCISES[key]
        summary.append(
            {
                "exercise": name,
                "bodyweight": bodyweight,
                "weeks": [
                    {
                        "label": bucket["label"],
                        "sessions": entry["sessions"][index],
                        "volume": _volume_text(entry["volumes"][index], bodyweight),
                        "best": _best_text(entry["bests"][index], bodyweight),
                    }
                    for index, bucket in enumerate(buckets)
                ],
            }
        )
    return summary


def _wording_context(
    payload: dict, profile: dict, cfg: Config, workouts: list[dict], today: date
) -> str:
    data = {
        "rules": _advice_rules(cfg),
        "profile": {
            "goal": profile.get("goal", ""),
            "days_per_week": profile.get("days_per_week"),
            "equipment": profile.get("equipment", ""),
            "injuries": profile.get("injuries", ""),
        },
        "summary": _training_summary(workouts, today),
        "candidates": [
            {"rule": item["rule"], "evidence": item["evidence"]}
            for item in payload["items"]
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _apply_wording(
    payload: dict,
    profile: dict,
    cfg: Config,
    client: llm.LLM,
    workouts: list[dict],
    today: date,
) -> dict:
    try:
        system = llm.load_prompt(cfg, "fitness-advice.md")
        raw = client.complete(
            system, _wording_context(payload, profile, cfg, workouts, today)
        )
        data = llm.parse_json(raw)
        order = data.get("order")
        raw_texts = data.get("texts")
        if not isinstance(order, list) or not isinstance(raw_texts, dict):
            return payload

        rules = [item["rule"] for item in payload["items"]]
        by_rule = {item["rule"]: item for item in payload["items"]}
        valid_order = (
            len(order) == len(rules)
            and all(isinstance(rule, str) for rule in order)
            and len(set(order)) == len(order)
            and set(order) == set(rules)
        )

        texts: dict[str, tuple[str, str]] = {}
        for rule, entry in raw_texts.items():
            if rule not in by_rule or not isinstance(entry, dict):
                return payload
            conclusion = entry.get("conclusion")
            action = entry.get("action")
            if (
                not isinstance(conclusion, str)
                or not isinstance(action, str)
                or not conclusion.strip()
                or not action.strip()
            ):
                return payload
            # 只拦阿拉伯数字；中文数词（如「多做一次」）是自然语言，模板本身也这么写。
            if any(char.isdigit() for char in conclusion + action):
                return payload
            texts[rule] = (conclusion.strip(), action.strip())

        ordered: list[dict] = []
        for rule in order if valid_order else rules:
            item = dict(by_rule[rule])
            if rule in texts:
                item["conclusion"], item["action"] = texts[rule]
            ordered.append(item)
        return {**payload, "llm_used": bool(texts) or valid_order, "items": ordered}
    except Exception:  # noqa: BLE001 - 模型失败只降级，不影响确定性建议
        return payload


def generate_advice(cfg: Config, client: llm.LLM | None = None) -> dict:
    today = date.fromisoformat(local_today())
    profile = read_profile(cfg)
    conn = store.connect(cfg)
    try:
        workouts = _window_workouts(conn, today)
        payload = _build_advice(workouts, profile, today)
        if client is not None and payload["items"]:
            payload = _apply_wording(payload, profile, cfg, client, workouts, today)
        conn.execute(
            "INSERT INTO advice_runs(created_at, payload, llm_used) VALUES (?, ?, ?)",
            (
                payload["generated_at"],
                json.dumps(payload, ensure_ascii=False),
                int(bool(payload["llm_used"])),
            ),
        )
        conn.execute(
            "DELETE FROM advice_runs WHERE id NOT IN "
            "(SELECT id FROM advice_runs ORDER BY id DESC LIMIT 20)"
        )
        conn.commit()
    finally:
        conn.close()
    return payload


def state(cfg: Config) -> dict:
    conn = store.connect(cfg)
    try:
        recent = _recent_workouts(conn)
        weights = _recent_weights(conn)
        last_advice = _last_advice(conn)
        pending = conn.execute(
            "SELECT COUNT(*) FROM corrections "
            "WHERE COALESCE(scope, 'mail') = 'fitness' AND status = 'pending'"
        ).fetchone()[0]
        demo = _has_demo(conn)
    finally:
        conn.close()
    return {
        "exercises": catalog(),
        "recent_workouts": recent,
        "weights": weights,
        "profile": read_profile(cfg),
        "last_advice": last_advice,
        "pending_corrections": int(pending),
        "has_demo": demo,
    }

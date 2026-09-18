"""业务服务层：观测入库、相邻区间重算、漂移补偿查询。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

from . import calibration
from .db import connect
from .models import epoch_to_iso


class DuplicateTimestampError(Exception):
    """同一时点已存在观测。"""


class ObservationNotFoundError(Exception):
    """指定 ID 的观测不存在。"""


class RevisionConflictError(Exception):
    """乐观锁版本过期，或目标时点已被其他观测占用。"""


class OutOfCoverageError(Exception):
    """查询时点超出已观测覆盖范围。"""

    def __init__(self, ts: float, lo: float | None, hi: float | None):
        self.ts, self.lo, self.hi = ts, lo, hi
        if lo is None:
            msg = "no observations yet; correction coverage is empty"
        else:
            msg = (
                f"timestamp {ts} is outside calibration coverage "
                f"[{lo}, {hi}] ({epoch_to_iso(lo)} .. {epoch_to_iso(hi)})"
            )
        super().__init__(msg)


# ---------- 观测 ----------

def create_observation(reference: float, tolerance: float, readings: list[float], ts: float) -> dict:
    bias, group_median, _inliers, _outliers = calibration.compute_bias(reference, tolerance, readings)
    with closing(connect()) as conn:
        with conn:  # 事务：观测、读数、区间重算要么全部成功要么全部回滚
            if conn.execute("SELECT 1 FROM observations WHERE ts = ?", (ts,)).fetchone():
                raise DuplicateTimestampError(f"an observation already exists at timestamp {ts}")
            cur = conn.execute(
                "INSERT INTO observations (ts, reference, tolerance, group_median, bias, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts, reference, tolerance, group_median, bias, time.time()),
            )
            obs_id = cur.lastrowid
            # 原始读数逐条保留，异常仅打标记，从不修改或删除
            conn.executemany(
                "INSERT INTO readings (observation_id, value, is_outlier) VALUES (?, ?, ?)",
                [
                    (obs_id, value, 1 if abs(value - group_median) > tolerance else 0)
                    for value in readings
                ],
            )
            _rebuild_adjacent_intervals(conn, ts)
            row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
            return _observation_dict(conn, row)


def _rebuild_adjacent_intervals(conn: sqlite3.Connection, ts: float) -> None:
    """新增观测后仅重算与其相邻的区间。

    被新时点一分为二的旧区间删除，再补建 (prev, ts) 与 (ts, next) 两段；
    其余区间不受影响，保持原样。
    """
    conn.execute("DELETE FROM intervals WHERE t_start < ? AND t_end > ?", (ts, ts))
    _insert_adjacent_intervals(conn, ts)


def _insert_adjacent_intervals(conn: sqlite3.Connection, ts: float) -> None:
    """补建以 ts 处观测为端点的两段相邻区间 (prev, ts)、(ts, next)，已存在则跳过。"""
    cur = conn.execute("SELECT id, ts, bias FROM observations WHERE ts = ?", (ts,)).fetchone()
    if cur is None:
        return
    prev = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()
    nxt = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts > ? ORDER BY ts ASC LIMIT 1", (ts,)
    ).fetchone()
    for a, b in ((prev, cur), (cur, nxt)):
        if a is None or b is None:
            continue
        exists = conn.execute(
            "SELECT 1 FROM intervals WHERE t_start = ? AND t_end = ?", (a["ts"], b["ts"])
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (a["ts"], b["ts"], a["bias"], b["bias"], a["id"], b["id"]),
            )


def revise_observation(
    obs_id: int,
    reference: float,
    tolerance: float,
    readings: list[float],
    ts: float,
    expected_revision: int,
) -> dict:
    """修订既有观测：快照、重判异常、重算偏差、重建原位置与新位置相邻区间，单事务完成。"""
    with closing(connect()) as conn:
        with conn:  # 快照、观测更新、读数替换、区间重建要么全部成功要么全部回滚
            row = conn.execute(
                "SELECT * FROM observations WHERE id = ?", (obs_id,)
            ).fetchone()
            if row is None:
                raise ObservationNotFoundError(f"observation {obs_id} not found")
            if row["revision"] != expected_revision:
                raise RevisionConflictError(
                    f"revision conflict: observation is at revision {row['revision']}, "
                    f"request carried {expected_revision}"
                )
            if ts != row["ts"] and conn.execute(
                "SELECT 1 FROM observations WHERE ts = ? AND id != ?", (ts, obs_id)
            ).fetchone():
                raise RevisionConflictError(
                    f"target timestamp {ts} is already occupied by another observation"
                )

            old_ts = row["ts"]
            # 用新输入重新判定异常值并计算偏差
            bias, group_median, _inliers, _outliers = calibration.compute_bias(
                reference, tolerance, readings
            )
            changed_at = time.time()
            old_readings = [
                {"value": r["value"], "is_outlier": bool(r["is_outlier"])}
                for r in conn.execute(
                    "SELECT value, is_outlier FROM readings"
                    " WHERE observation_id = ? ORDER BY id",
                    (obs_id,),
                ).fetchall()
            ]

            # 先保存修订前快照（记录旧版本号、旧输入、旧偏差）
            conn.execute(
                "INSERT INTO observation_revisions (observation_id, revision, changed_at,"
                " old_ts, old_reference, old_tolerance, old_group_median, old_bias, old_readings)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    row["revision"],
                    changed_at,
                    old_ts,
                    row["reference"],
                    row["tolerance"],
                    row["group_median"],
                    row["bias"],
                    json.dumps(old_readings),
                ),
            )
            # 观测 ID 保持不变，仅更新内容并推进版本号
            conn.execute(
                "UPDATE observations SET ts = ?, reference = ?, tolerance = ?,"
                " group_median = ?, bias = ?, revision = revision + 1 WHERE id = ?",
                (ts, reference, tolerance, group_median, bias, obs_id),
            )
            # 读数以新输入整体替换；旧读数已留存于修订快照
            conn.execute("DELETE FROM readings WHERE observation_id = ?", (obs_id,))
            conn.executemany(
                "INSERT INTO readings (observation_id, value, is_outlier) VALUES (?, ?, ?)",
                [
                    (obs_id, value, 1 if abs(value - group_median) > tolerance else 0)
                    for value in readings
                ],
            )
            _repair_intervals_after_revision(conn, old_ts, ts)
            updated = conn.execute(
                "SELECT * FROM observations WHERE id = ?", (obs_id,)
            ).fetchone()
            return _observation_dict(conn, updated)


def _repair_intervals_after_revision(
    conn: sqlite3.Connection, old_ts: float, new_ts: float
) -> None:
    """修订后重建受影响的相邻插值区间。

    删除原位置与新位置触及的区间（含移动后以被修订观测为端点的区间），
    再连接原位置移走后两侧的最近邻，并在新位置拆分出两段新区间；
    移动路径中间未涉及的区间原样保留。
    """
    conn.execute(
        "DELETE FROM intervals WHERE (t_start <= ? AND t_end >= ?)"
        " OR (t_start <= ? AND t_end >= ?)",
        (old_ts, old_ts, new_ts, new_ts),
    )
    if old_ts != new_ts:
        _bridge_empty_point(conn, old_ts)
    _insert_adjacent_intervals(conn, new_ts)


def _bridge_empty_point(conn: sqlite3.Connection, ts: float) -> None:
    """时点 ts 处的观测移走后，若两侧最近邻之间再无其他观测，则补一段跨越缺口的区间。"""
    prev = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()
    nxt = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts > ? ORDER BY ts ASC LIMIT 1", (ts,)
    ).fetchone()
    if prev is None or nxt is None:
        return
    between = conn.execute(
        "SELECT 1 FROM observations WHERE ts > ? AND ts < ? LIMIT 1", (prev["ts"], nxt["ts"])
    ).fetchone()
    if between:
        return  # 两侧最近邻并不相邻（被修订观测落在中间），由新位置拆区间处理
    exists = conn.execute(
        "SELECT 1 FROM intervals WHERE t_start = ? AND t_end = ?", (prev["ts"], nxt["ts"])
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (prev["ts"], nxt["ts"], prev["bias"], nxt["bias"], prev["id"], nxt["id"]),
        )


def list_observations() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM observations ORDER BY ts").fetchall()
        return [_observation_dict(conn, row) for row in rows]


def list_revisions(obs_id: int) -> list[dict]:
    """按版本倒序返回修订前快照；观测不存在时抛 ObservationNotFoundError。"""
    with closing(connect()) as conn:
        if not conn.execute(
            "SELECT 1 FROM observations WHERE id = ?", (obs_id,)
        ).fetchone():
            raise ObservationNotFoundError(f"observation {obs_id} not found")
        rows = conn.execute(
            "SELECT * FROM observation_revisions WHERE observation_id = ?"
            " ORDER BY revision DESC",
            (obs_id,),
        ).fetchall()
        return [_revision_dict(row) for row in rows]


def _revision_dict(row: sqlite3.Row) -> dict:
    old_readings = json.loads(row["old_readings"])
    return {
        "revision": row["revision"],
        "changed_at": row["changed_at"],
        "changed_at_iso": epoch_to_iso(row["changed_at"]),
        "old_timestamp": row["old_ts"],
        "old_timestamp_iso": epoch_to_iso(row["old_ts"]),
        "old_reference": row["old_reference"],
        "old_tolerance": row["old_tolerance"],
        "old_readings": [
            {"value": r["value"], "is_outlier": bool(r["is_outlier"])} for r in old_readings
        ],
        "old_group_median": row["old_group_median"],
        "old_bias": row["old_bias"],
    }


def _observation_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    readings = conn.execute(
        "SELECT value, is_outlier FROM readings WHERE observation_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "timestamp": row["ts"],
        "timestamp_iso": epoch_to_iso(row["ts"]),
        "reference": row["reference"],
        "tolerance": row["tolerance"],
        "group_median": row["group_median"],
        "bias": row["bias"],
        "revision": row["revision"],
        "readings": [{"value": r["value"], "is_outlier": bool(r["is_outlier"])} for r in readings],
        "outliers": [r["value"] for r in readings if r["is_outlier"]],
    }


# ---------- 区间 ----------

def list_intervals() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM intervals ORDER BY t_start").fetchall()
        return [_interval_dict(row) for row in rows]


def _interval_dict(row: sqlite3.Row) -> dict:
    return {
        "t_start": row["t_start"],
        "t_end": row["t_end"],
        "start_iso": epoch_to_iso(row["t_start"]),
        "end_iso": epoch_to_iso(row["t_end"]),
        "bias_start": row["bias_start"],
        "bias_end": row["bias_end"],
    }


# ---------- 漂移补偿 ----------

def correct(ts: float, reading: float) -> dict:
    """对待校正读数施加所在时点的插值偏差，返回校正结果与区间、异常明细。"""
    with closing(connect()) as conn:
        exact = conn.execute("SELECT * FROM observations WHERE ts = ?", (ts,)).fetchone()
        if exact is not None:
            # 恰落在观测点上：直接使用该点偏差，无需插值
            bias = exact["bias"]
            interval = None
            endpoints = [exact]
        else:
            row = conn.execute(
                "SELECT * FROM intervals WHERE t_start <= ? AND t_end >= ?", (ts, ts)
            ).fetchone()
            if row is None:
                bounds = conn.execute("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM observations").fetchone()
                raise OutOfCoverageError(ts, bounds["lo"], bounds["hi"])
            bias = calibration.interpolate(ts, row["t_start"], row["bias_start"], row["t_end"], row["bias_end"])
            interval = _interval_dict(row)
            endpoints = [
                conn.execute("SELECT * FROM observations WHERE id = ?", (row["obs_start_id"],)).fetchone(),
                conn.execute("SELECT * FROM observations WHERE id = ?", (row["obs_end_id"],)).fetchone(),
            ]
        return {
            "timestamp": ts,
            "timestamp_iso": epoch_to_iso(ts),
            "reading": reading,
            "bias": bias,
            "corrected": reading + bias,
            "interval": interval,
            "endpoints": [_endpoint_dict(conn, ob) for ob in endpoints],
        }


def _endpoint_dict(conn: sqlite3.Connection, obs: sqlite3.Row) -> dict:
    outliers = conn.execute(
        "SELECT value FROM readings WHERE observation_id = ? AND is_outlier = 1 ORDER BY id",
        (obs["id"],),
    ).fetchall()
    return {
        "observation_id": obs["id"],
        "timestamp": obs["ts"],
        "timestamp_iso": epoch_to_iso(obs["ts"]),
        "bias": obs["bias"],
        "outliers": [r["value"] for r in outliers],
    }

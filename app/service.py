"""业务服务层：观测入库、相邻区间重算、漂移补偿查询。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

from . import calibration
from .db import connect
from .models import epoch_to_iso


class DuplicateTimestampError(Exception):
    """同一时点已存在观测。"""


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
    cur = conn.execute("SELECT id, ts, bias FROM observations WHERE ts = ?", (ts,)).fetchone()
    prev = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
    ).fetchone()
    nxt = conn.execute(
        "SELECT id, ts, bias FROM observations WHERE ts > ? ORDER BY ts ASC LIMIT 1", (ts,)
    ).fetchone()
    if prev:
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (prev["ts"], ts, prev["bias"], cur["bias"], prev["id"], cur["id"]),
        )
    if nxt:
        conn.execute(
            "INSERT INTO intervals (t_start, t_end, bias_start, bias_end, obs_start_id, obs_end_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, nxt["ts"], cur["bias"], nxt["bias"], cur["id"], nxt["id"]),
        )


def list_observations() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM observations ORDER BY ts").fetchall()
        return [_observation_dict(conn, row) for row in rows]


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

"""FastAPI 入口：仪器校准漂移补偿 API。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import service
from .calibration import CalibrationError
from .db import init_db
from .models import CorrectionIn, CorrectionOut, IntervalOut, ObservationIn, ObservationOut
from .service import DuplicateTimestampError, OutOfCoverageError


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Instrument Calibration Drift Compensation API",
    version="1.0.0",
    description=(
        "提交参考值与同一时点至少 3 次读数，服务标记异常读数并计算校准偏差；"
        "在相邻观测时点间线性插值偏差，对待校正读数施加补偿。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/observations", response_model=ObservationOut, status_code=201)
def post_observation(payload: ObservationIn) -> dict:
    """登记一次校准观测：标记异常读数、计算偏差、重算相邻区间。"""
    try:
        return service.create_observation(
            reference=payload.reference,
            tolerance=payload.tolerance,
            readings=payload.readings,
            ts=payload.timestamp,
        )
    except DuplicateTimestampError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/observations", response_model=list[ObservationOut])
def get_observations() -> list[dict]:
    """按时点列出全部观测（含原始读数与异常标记）。"""
    return service.list_observations()


@app.get("/intervals", response_model=list[IntervalOut])
def get_intervals() -> list[dict]:
    """列出当前相邻观测时点构成的插值区间。"""
    return service.list_intervals()


@app.post("/corrections", response_model=CorrectionOut)
def post_correction(payload: CorrectionIn) -> dict:
    """对指定时点的读数做漂移补偿；覆盖范围外拒绝。"""
    try:
        return service.correct(ts=payload.timestamp, reading=payload.reading)
    except OutOfCoverageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

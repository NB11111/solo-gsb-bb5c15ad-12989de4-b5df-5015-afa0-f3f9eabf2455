"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALIB_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app  # 环境变量设置后再导入

    with TestClient(app) as c:
        yield c


def make_observation(client, ts, reference, tolerance, readings):
    resp = client.post(
        "/observations",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance, "readings": readings},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 观测登记 ----------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_observation_marks_outliers_and_computes_bias(client):
    body = make_observation(client, 1000.0, reference=10.0, tolerance=0.5,
                            readings=[9.0, 9.1, 8.9, 20.0])
    # 组中位数 9.05，20.0 偏离超差判为异常；有效读数中位数 9.0 → 偏差 10.0 - 9.0 = 1.0
    assert body["group_median"] == pytest.approx(9.05)
    assert body["bias"] == pytest.approx(1.0)
    assert body["outliers"] == [20.0]
    flags = {r["value"]: r["is_outlier"] for r in body["readings"]}
    assert flags == {9.0: False, 9.1: False, 8.9: False, 20.0: True}


def test_raw_readings_are_preserved(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9, 20.0])
    obs = client.get("/observations").json()
    assert len(obs) == 1
    assert sorted(r["value"] for r in obs[0]["readings"]) == [8.9, 9.0, 9.1, 20.0]


def test_fewer_than_three_readings_rejected(client):
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.1,
                             "readings": [1.0, 1.1]})
    assert resp.status_code == 422


def test_all_outliers_rejected(client):
    # 容差为 0 时四个读数两两不同，全部偏离中位数 → 无法计算偏差
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 1.0, "tolerance": 0.0,
                             "readings": [1.0, 2.0, 3.0, 4.0]})
    assert resp.status_code == 422
    assert "cannot compute a calibration bias" in resp.json()["detail"]


def test_duplicate_timestamp_rejected(client):
    make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    resp = client.post("/observations",
                       json={"timestamp": 1000.0, "reference": 10.0, "tolerance": 0.5,
                             "readings": [9.0, 9.1, 8.9]})
    assert resp.status_code == 409


def test_iso8601_timestamp_accepted(client):
    body = make_observation(client, "1970-01-01T00:16:40Z", 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["timestamp"] == pytest.approx(1000.0)


# ---------- 漂移补偿 ----------

@pytest.fixture()
def two_points(client):
    make_observation(client, 1000.0, reference=10.0, tolerance=0.5, readings=[9.0, 9.1, 8.9, 20.0])  # bias 1.0
    make_observation(client, 2000.0, reference=10.0, tolerance=0.5, readings=[7.0, 7.1, 6.9])        # bias 3.0
    return client


def test_correction_interpolates_linearly(two_points):
    resp = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bias"] == pytest.approx(2.0)        # 1.0 与 3.0 的中点
    assert body["corrected"] == pytest.approx(7.0)   # 读数 + 偏差
    assert body["interval"]["t_start"] == 1000.0
    assert body["interval"]["t_end"] == 2000.0
    assert body["interval"]["bias_start"] == pytest.approx(1.0)
    assert body["interval"]["bias_end"] == pytest.approx(3.0)


def test_correction_reports_endpoint_anomalies(two_points):
    body = two_points.post("/corrections", json={"timestamp": 1500.0, "reading": 5.0}).json()
    assert len(body["endpoints"]) == 2
    assert body["endpoints"][0]["outliers"] == [20.0]
    assert body["endpoints"][1]["outliers"] == []


def test_correction_at_exact_observation_point(two_points):
    body = two_points.post("/corrections", json={"timestamp": 2000.0, "reading": 5.0}).json()
    assert body["bias"] == pytest.approx(3.0)
    assert body["corrected"] == pytest.approx(8.0)
    assert body["interval"] is None
    assert len(body["endpoints"]) == 1


def test_correction_outside_coverage_rejected(two_points):
    for ts in (999.0, 2001.0):
        resp = two_points.post("/corrections", json={"timestamp": ts, "reading": 5.0})
        assert resp.status_code == 400
        assert "outside calibration coverage" in resp.json()["detail"]


def test_correction_without_any_observation_rejected(client):
    resp = client.post("/corrections", json={"timestamp": 1.0, "reading": 5.0})
    assert resp.status_code == 400


# ---------- 区间重算 ----------

def test_adjacent_intervals_recomputed_after_insert(two_points):
    # 在 1000 与 2000 之间插入新观测（bias 2.0）
    make_observation(two_points, 1500.0, reference=10.0, tolerance=0.5, readings=[8.0, 8.1, 7.9])
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1000.0, 1500.0), (1500.0, 2000.0)]

    # 旧区间中点 1250 现在应基于新区间 (1000,1500) 插值：bias = 1.5
    body = two_points.post("/corrections", json={"timestamp": 1250.0, "reading": 10.0}).json()
    assert body["bias"] == pytest.approx(1.5)
    assert body["corrected"] == pytest.approx(11.5)


def test_intervals_extend_at_edges(two_points):
    make_observation(two_points, 500.0, 10.0, 0.5, [9.5, 9.6, 9.4])   # bias 0.5
    make_observation(two_points, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])  # bias 4.0
    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(500.0, 1000.0), (1000.0, 2000.0), (2000.0, 3000.0)]
    # 覆盖范围随之扩展
    assert two_points.post("/corrections", json={"timestamp": 750.0, "reading": 0.0}).status_code == 200
    assert two_points.post("/corrections", json={"timestamp": 2500.0, "reading": 0.0}).status_code == 200


# ---------- 观测修订 ----------

def revise(client, obs_id, ts, reference, tolerance, readings, revision):
    resp = client.put(
        f"/observations/{obs_id}",
        json={"timestamp": ts, "reference": reference, "tolerance": tolerance,
              "readings": readings, "revision": revision},
    )
    return resp


def test_new_observation_exposes_revision_one(client):
    body = make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])
    assert body["revision"] == 1


def test_revise_in_place_rejudges_and_rebuilds(two_points):
    obs = two_points.get("/observations").json()[0]   # ts=1000, bias=1.0
    resp = revise(two_points, obs["id"], 1000.0, reference=12.0, tolerance=0.5,
                  readings=[9.0, 9.1, 8.9, 30.0], revision=1)
    # 30.0 偏离中位数 9.05 判为异常；bias = 12.0 - 9.0 = 3.0
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == obs["id"]                    # ID 保持不变
    assert body["revision"] == 2
    assert body["bias"] == pytest.approx(3.0)
    assert body["outliers"] == [30.0]

    # 区间端点偏差随修订更新：(1000,2000) 的 bias 从 1.0→3.0
    intervals = two_points.get("/intervals").json()
    assert [(iv["t_start"], iv["t_end"]) for iv in intervals] == [(1000.0, 2000.0)]
    assert intervals[0]["bias_start"] == pytest.approx(3.0)
    assert intervals[0]["bias_end"] == pytest.approx(3.0)

    corrected = two_points.post("/corrections",
                                json={"timestamp": 1500.0, "reading": 5.0}).json()
    assert corrected["bias"] == pytest.approx(3.0)


def test_revise_move_timestamp_rebuilds_old_and_new_intervals(two_points):
    obs = two_points.get("/observations").json()[0]   # ts=1000, bias=1.0
    # 把 1000 处观测移到 1500，并把偏差改为 2.0（读数中位数 8.0）
    resp = revise(two_points, obs["id"], 1500.0, reference=10.0, tolerance=0.5,
                  readings=[8.0, 8.1, 7.9], revision=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == obs["id"]
    assert resp.json()["revision"] == 2

    spans = [(iv["t_start"], iv["t_end"]) for iv in two_points.get("/intervals").json()]
    assert spans == [(1500.0, 2000.0)]                # 旧位置不再产生 (1000,2000)
    body = two_points.post("/corrections",
                           json={"timestamp": 1750.0, "reading": 0.0}).json()
    assert body["bias"] == pytest.approx(2.5)         # 2.0 与 3.0 中点
    # 移走后 1000 落在覆盖范围之外
    assert two_points.post("/corrections",
                           json={"timestamp": 1000.0, "reading": 0.0}).status_code == 400


def test_revise_move_into_middle_of_gap_splits_interval(client):
    a = make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])  # bias 1.0
    make_observation(client, 3000.0, 10.0, 0.5, [6.0, 6.1, 5.9])      # bias 4.0
    resp = revise(client, a["id"], 2000.0, reference=10.0, tolerance=0.5,
                  readings=[8.0, 8.1, 7.9], revision=1)               # bias 2.0
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"]) for iv in client.get("/intervals").json()]
    assert spans == [(2000.0, 3000.0)]


def test_revise_move_across_other_observations(client):
    a = make_observation(client, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9])  # bias 1.0
    make_observation(client, 2000.0, 10.0, 0.5, [8.0, 8.1, 7.9])      # bias 2.0
    make_observation(client, 3000.0, 10.0, 0.5, [7.0, 7.1, 6.9])      # bias 3.0
    make_observation(client, 4000.0, 10.0, 0.5, [6.0, 6.1, 5.9])      # bias 4.0
    # 1000(bias1) 移到 3500：旧位置由 2000 接续，新位置拆分 (3000,4000)
    resp = revise(client, a["id"], 3500.0, reference=10.0, tolerance=0.5,
                  readings=[9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 200, resp.text
    spans = [(iv["t_start"], iv["t_end"])
             for iv in client.get("/intervals").json()]
    assert spans == [(2000.0, 3000.0), (3000.0, 3500.0), (3500.0, 4000.0)]
    biases = {iv["t_start"]: iv["bias_start"]
              for iv in client.get("/intervals").json()}
    assert biases[3000.0] == pytest.approx(3.0)


def test_stale_revision_conflicts(two_points):
    obs = two_points.get("/observations").json()[0]
    assert revise(two_points, obs["id"], 1000.0, 10.0, 0.5,
                  [9.0, 9.1, 8.9], revision=1).status_code == 200
    # 仍携带 revision=1 的第二次修订必须冲突
    resp = revise(two_points, obs["id"], 1000.0, 10.0, 0.5,
                  [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 409
    assert "revision conflict" in resp.json()["detail"]
    # 带最新 revision 可继续修订
    assert revise(two_points, obs["id"], 1000.0, 10.0, 0.5,
                  [9.0, 9.1, 8.9], revision=2).status_code == 200


def test_revise_to_occupied_timestamp_conflicts(two_points):
    obs = two_points.get("/observations").json()[0]   # ts=1000
    resp = revise(two_points, obs["id"], 2000.0, 10.0, 0.5,
                  [9.0, 9.1, 8.9], revision=1)        # 2000 已被另一观测占用
    assert resp.status_code == 409
    assert "already occupied" in resp.json()["detail"]
    # 冲突时数据不变：revision 仍为 1，区间不变
    assert two_points.get("/observations").json()[0]["revision"] == 1


def test_revise_unknown_id_returns_404(client):
    resp = revise(client, 999, 1000.0, 10.0, 0.5, [9.0, 9.1, 8.9], revision=1)
    assert resp.status_code == 404


def test_revise_invalid_inputs_returns_422(two_points):
    obs = two_points.get("/observations").json()[0]
    resp = revise(two_points, obs["id"], 1000.0, 10.0, 0.0,
                  [1.0, 2.0, 3.0, 4.0], revision=1)
    assert resp.status_code == 422


def test_revisions_listed_newest_first(two_points):
    obs = two_points.get("/observations").json()[0]
    revise(two_points, obs["id"], 1000.0, reference=11.0, tolerance=0.5,
           readings=[9.0, 9.1, 8.9], revision=1)                        # bias 2.0
    revise(two_points, obs["id"], 1000.0, reference=12.0, tolerance=0.5,
           readings=[9.0, 9.1, 8.9], revision=2)                        # bias 3.0

    resp = two_points.get(f"/observations/{obs['id']}/revisions")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert [item["revision"] for item in items] == [2, 1]               # 版本倒序

    first = items[0]
    assert first["old_reference"] == 11.0                               # 第二次修订前快照
    assert first["old_bias"] == pytest.approx(2.0)
    assert first["old_tolerance"] == 0.5
    assert [r["value"] for r in first["old_readings"]] == [9.0, 9.1, 8.9]
    assert "changed_at" in first and "changed_at_iso" in first

    initial = items[1]
    assert initial["old_reference"] == 10.0                             # 首次登记时的输入
    assert initial["old_bias"] == pytest.approx(1.0)


def test_revisions_capture_outlier_flags_and_old_timestamp(two_points):
    obs = two_points.get("/observations").json()[0]
    revise(two_points, obs["id"], 1000.0, 10.0, 0.5,
           [9.0, 9.1, 8.9, 20.0], revision=1)
    revise(two_points, obs["id"], 1500.0, 10.0, 0.5,
           [9.0, 9.1, 8.9], revision=2)
    items = two_points.get(f"/observations/{obs['id']}/revisions").json()
    newest = items[0]
    assert newest["old_timestamp"] == pytest.approx(1000.0)
    flags = {r["value"]: r["is_outlier"] for r in newest["old_readings"]}
    assert flags == {9.0: False, 9.1: False, 8.9: False, 20.0: True}


def test_revisions_empty_for_unrevised_observation(two_points):
    obs = two_points.get("/observations").json()[0]
    assert two_points.get(f"/observations/{obs['id']}/revisions").json() == []


def test_revisions_unknown_id_returns_404(client):
    assert client.get("/observations/999/revisions").status_code == 404

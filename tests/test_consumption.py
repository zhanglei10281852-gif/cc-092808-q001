from __future__ import annotations

import sqlite3
import threading

import pytest

from app.core.errors import ConflictError, ValidationError
from app.database import database_path, transaction
from app.forensics.service import ForensicService

from tests.test_forensics_workflow import create_accepted_forensic_case


def _setup_lot(service: ForensicService, suffix: str, containers: list[tuple[str, float]]):
    """建案件、库位、检材，并按给定 (容器编码, 数量) 摆放多个容器。"""
    forensic_case = create_accepted_forensic_case(service, suffix)
    location = service.custody.create_location({
        "location_code": f"VAULT-{suffix}", "facility": "检材保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    })
    total = sum(quantity for _, quantity in containers)
    specimen = service.custody.create_specimen({
        "specimen_no": f"SP-{suffix}", "case_id": forensic_case["id"], "parent_specimen_id": None,
        "received_year": 2026, "initial_quantity": total, "integrity_percent": 100,
        "packaging": "防拆封袋，封识完整", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    placements = []
    for index, (code, quantity) in enumerate(containers, start=1):
        result = service.custody.place_specimen({
            "specimen_id": specimen["id"], "location_id": location["id"], "quantity": quantity,
            "container_code": code, "idempotency_key": f"place-{suffix}-{index:04d}", "actor": "保管员",
        })
        placements.append(result["placement"])
    return service.repository.specimen_detail(specimen_id := specimen["id"]), location, placements


def test_withdrawal_decreases_ledger_containers_and_location_together(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, location, _ = _setup_lot(service, "W01", [("BOX-W01-A", 12), ("BOX-W01-B", 8)])

        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
            "idempotency_key": "draw-W01-0001", "actor": "鉴定技术员", "reason": "DNA 提取取样",
        })
        assert result["replayed"] is False
        # 总账、容器、库位占用始终是同一份剩余量 15
        assert result["specimen"]["available_quantity"] == 15
        detail = service.repository.specimen_detail(specimen["id"])
        active = [p for p in detail["placements"] if p["removed_at"] is None]
        assert sorted(p["quantity"] for p in active) == [7, 8]
        location_view = service.repository.location_detail(location["id"])
        assert location_view["used_grams"] == 15
        assert location_view["available_grams"] == 985

        report = service.custody.reconcile(specimen["id"])
        assert report["conserved"] is True
        assert report["active_placement_grams"] == 15
        assert report["consumed_grams"] == 5
        # 每一份耗用都能追溯到具体封装
        assert len(result["consumptions"]) == 1
        assert result["consumptions"][0]["container_code"] == "BOX-W01-A"
        assert result["consumptions"][0]["quantity"] == 5


def test_auto_allocation_uses_stable_fifo_order(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W02", [("BOX-W02-A", 4), ("BOX-W02-B", 4), ("BOX-W02-C", 4)])

        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 6, "movement_type": "取样",
            "idempotency_key": "draw-W02-0001", "actor": "鉴定技术员", "reason": "跨封装取样六份",
        })
        taken = {(c["container_code"], c["quantity"]) for c in result["consumptions"]}
        # 先进先出：最早入库的容器先取空，再从下一个容器取
        assert taken == {("BOX-W02-A", 4), ("BOX-W02-B", 2)}
        assert placements[0]["id"] == result["consumptions"][0]["placement_id"]
        report = service.custody.reconcile(specimen["id"])
        assert report["conserved"] is True


def test_explicit_allocations_deduct_named_containers(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W03", [("BOX-W03-A", 5), ("BOX-W03-B", 5)])

        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 7, "movement_type": "领用",
            "idempotency_key": "draw-W03-0001", "actor": "保管员", "reason": "实验室领用七份",
            "allocations": [
                {"placement_id": placements[1]["id"], "quantity": 5},
                {"placement_id": placements[0]["id"], "quantity": 2},
            ],
        })
        taken = {c["placement_id"]: c["quantity"] for c in result["consumptions"]}
        assert taken == {placements[1]["id"]: 5, placements[0]["id"]: 2}
        detail = service.repository.specimen_detail(specimen["id"])
        by_id = {p["id"]: p for p in detail["placements"]}
        assert by_id[placements[0]["id"]]["quantity"] == 3
        assert by_id[placements[1]["id"]]["quantity"] == 0
        assert by_id[placements[1]["id"]]["removed_at"] is not None
        assert service.custody.reconcile(specimen["id"])["conserved"] is True


def test_allocations_must_sum_to_total(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W04", [("BOX-W04-A", 5), ("BOX-W04-B", 5)])
        with pytest.raises(ValidationError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 7, "movement_type": "领用",
                "idempotency_key": "draw-W04-bad", "actor": "保管员", "reason": "分配合计对不上",
                "allocations": [{"placement_id": placements[0]["id"], "quantity": 6}],
            })


def test_insufficient_container_rolls_back_entire_withdrawal(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W05", [("BOX-W05-A", 3), ("BOX-W05-B", 7)])
        connection.execute("SAVEPOINT expect_fail")
        try:
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
                "idempotency_key": "draw-W05-fail", "actor": "鉴定技术员", "reason": "指定容器不够",
                "allocations": [{"placement_id": placements[0]["id"], "quantity": 5}],
            })
        except ConflictError:
            connection.execute("ROLLBACK TO SAVEPOINT expect_fail")
        else:
            raise AssertionError("容器不足应令整次耗用回滚")
        connection.execute("RELEASE SAVEPOINT expect_fail")
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 10
        assert {p["id"]: p["quantity"] for p in detail["placements"]} == {
            placements[0]["id"]: 3, placements[1]["id"]: 7,
        }
        assert service.repository.custody_event_by_key("draw-W05-fail") is None


def test_withdrawal_from_removed_container_is_rejected(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, location, placements = _setup_lot(service, "W06", [("BOX-W06-A", 5), ("BOX-W06-B", 5)])
        other = service.custody.create_location({
            "location_code": "VAULT-W06-2", "facility": "检材保管室", "room": "恒温区", "rack": "R2", "shelf": "S1",
            "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
        })
        service.custody.move_placement(placements[0]["id"], {
            "target_location_id": other["id"], "expected_version": 1,
            "idempotency_key": "move-W06-0001", "actor": "保管员", "reason": "库位整理",
        })
        connection.execute("SAVEPOINT expect_fail")
        try:
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 2, "movement_type": "取样",
                "idempotency_key": "draw-W06-fail", "actor": "鉴定技术员", "reason": "指向已移出容器",
                "allocations": [{"placement_id": placements[0]["id"], "quantity": 2, "expected_version": 2}],
            })
        except ConflictError:
            connection.execute("ROLLBACK TO SAVEPOINT expect_fail")
        else:
            raise AssertionError("已移出的容器必须回滚整次耗用")
        connection.execute("RELEASE SAVEPOINT expect_fail")
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 10
        # 原库位仅剩未移动的 BOX-W06-B（5 份），被指向的容器在另一库位且未被扣减
        assert service.repository.location_usage(location["id"]) == 5
        assert service.repository.location_usage(other["id"]) == 5


def test_stale_container_version_rolls_back(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W07", [("BOX-W07-A", 10)])
        connection.execute("SAVEPOINT expect_fail")
        try:
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 2, "movement_type": "取样",
                "idempotency_key": "draw-W07-fail", "actor": "鉴定技术员", "reason": "版本过期",
                "allocations": [{"placement_id": placements[0]["id"], "quantity": 2, "expected_version": 99}],
            })
        except ConflictError:
            connection.execute("ROLLBACK TO SAVEPOINT expect_fail")
        else:
            raise AssertionError("容器版本过期必须回滚整次耗用")
        connection.execute("RELEASE SAVEPOINT expect_fail")
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 10
        assert detail["placements"][0]["version"] == 1
        assert detail["placements"][0]["quantity"] == 10


def test_replayed_same_key_does_not_deduct_again(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W08", [("BOX-W08-A", 10)])
        payload = {
            "specimen_id": specimen["id"], "quantity": 3, "movement_type": "取样",
            "idempotency_key": "draw-W08-0001", "actor": "鉴定技术员", "reason": "网络重试验证",
            "allocations": [{"placement_id": placements[0]["id"], "quantity": 3}],
        }
        first = service.custody.withdraw(payload)
        second = service.custody.withdraw(payload)
        assert first["replayed"] is False and second["replayed"] is True
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 7
        assert detail["placements"][0]["quantity"] == 7
        assert len(detail["consumptions"]) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"quantity": 4},
        {"reason": "用途被改写"},
        {"allocations": None},  # 原始为显式分配，重试改为自动分配
    ],
)
def test_same_key_with_different_content_reports_conflict(client, mutation):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, placements = _setup_lot(service, "W09", [("BOX-W09-A", 10)])
        payload = {
            "specimen_id": specimen["id"], "quantity": 3, "movement_type": "取样",
            "idempotency_key": "draw-W09-0001", "actor": "鉴定技术员", "reason": "原始用途",
            "allocations": [{"placement_id": placements[0]["id"], "quantity": 3}],
        }
        service.custody.withdraw(payload)
        retried = dict(payload)
        if "allocations" in mutation and mutation["allocations"] is None:
            retried.pop("allocations")
        else:
            retried.update(mutation)
        with pytest.raises(ConflictError):
            service.custody.withdraw(retried)
        # 冲突请求不得产生第二次扣减
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 7
        assert len(detail["consumptions"]) == 1


def test_disposal_empties_container_and_marks_disposed(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, location, placements = _setup_lot(service, "W10", [("BOX-W10-A", 6)])
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 6, "movement_type": "报废",
            "idempotency_key": "draw-W10-0001", "actor": "保管员", "reason": "污染报废全部销毁",
        })
        assert result["specimen"]["status"] == "disposed"
        assert result["specimen"]["available_quantity"] == 0
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["placements"][0]["removed_at"] is not None
        assert service.repository.location_usage(location["id"]) == 0
        assert service.custody.reconcile(specimen["id"])["conserved"] is True


def test_hold_is_never_bypassed_and_leaves_quantities_untouched(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, _ = _setup_lot(service, "W11", [("BOX-W11-A", 10)])
        service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "保全", "reason": "等待法院裁定", "actor": "审核员",
        })
        connection.execute("SAVEPOINT expect_fail")
        try:
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 4, "movement_type": "领用",
                "idempotency_key": "draw-W11-fail", "actor": "保管员", "reason": "冻结期间领用",
            })
        except ConflictError:
            connection.execute("ROLLBACK TO SAVEPOINT expect_fail")
        else:
            raise AssertionError("冻结检材不得耗用")
        connection.execute("RELEASE SAVEPOINT expect_fail")
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 10
        assert detail["placements"][0]["quantity"] == 10


def test_concurrent_consumptions_never_go_negative(client):
    # 先提交一批基础数据，供两个独立连接并发耗用
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, _ = _setup_lot(service, "W12", [("BOX-W12-A", 500)])
        specimen_id = specimen["id"]

    path = database_path()
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(key: str) -> None:
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            barrier.wait()
            conn.execute("BEGIN IMMEDIATE")
            ForensicService(conn).custody.withdraw({
                "specimen_id": specimen_id, "quantity": 300, "movement_type": "取样",
                "idempotency_key": key, "actor": "鉴定技术员", "reason": "并发耗用各三百份",
            })
            conn.execute("COMMIT")
            outcomes.append("ok")
        except ConflictError:
            conn.execute("ROLLBACK")
            outcomes.append("conflict")
        except Exception:  # pragma: no cover - 测试失败时暴露
            conn.execute("ROLLBACK")
            outcomes.append("error")
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=("draw-W12-t1",)),
        threading.Thread(target=worker, args=("draw-W12-t2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        detail = service.repository.specimen_detail(specimen_id)
        assert detail["available_quantity"] == 200
        assert detail["placements"][0]["quantity"] == 200
        consumed = [c for c in detail["consumptions"]]
        assert len(consumed) == 1 and consumed[0]["quantity"] == 300
        assert service.custody.reconcile(specimen_id)["conserved"] is True


def test_release_fulfillment_deducts_containers_and_is_idempotent(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        specimen, _, _ = _setup_lot(service, "W13", [("BOX-W13-A", 500)])
        case_id = specimen["forensic_case"]["id"]
        request = service.release.create_request({
            "request_no": "REL-W13", "requester": "法医物证实验室", "purpose": "补充检验领用二十份",
            "items": [{"case_id": case_id, "quantity": 20}],
        })
        submitted = service.release.submit(request["id"], 1)
        service.release.decide(request["id"], {
            "approve": True, "expected_version": submitted["version"], "actor": "案件审核员", "reason": "材料充足",
        })
        fulfillment = service.release.fulfill(request["id"], {"actor": "保管员"})
        assert fulfillment["replayed"] is False
        assert fulfillment["request"]["status"] == "fulfilled"
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 480
        assert detail["placements"][0]["quantity"] == 480
        # 发放接口重试不得重复扣减
        replay = service.release.fulfill(request["id"], {"actor": "保管员"})
        assert replay["replayed"] is True
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 480
        assert service.custody.reconcile(specimen["id"])["conserved"] is True


def test_http_withdrawal_and_read_views_share_one_remaining(client, admin):
    headers = admin["headers"]
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "ORG-W14", "agency_name": "市公安局物证中心", "jurisdiction_code": "CN",
        "contact_address": "司法路 1 号", "restrictions": {},
    })
    case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "CASE-W14", "case_name": "检材扣减一体性", "discipline": "法医物证",
        "entrusted_matter": "同一剩余量核对", "agency_id": source.json()["id"], "case_source": "委托",
        "accepted_on": "2026-09-01", "passport": {}, "created_by": "登记员",
    })
    client.post(f"/api/forensics/cases/{case.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "LOC-W14", "facility": "保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    }).json()
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "SP-W14", "case_id": case.json()["id"], "received_year": 2026,
        "initial_quantity": 20, "integrity_percent": 100, "packaging": "二十份独立封装", "created_by": "登记员",
    }).json()
    placement = client.post("/api/forensics/placements", headers=headers, json={
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 20,
        "container_code": "BOX-W14", "idempotency_key": "place-W14-0001", "actor": "保管员",
    }).json()["placement"]

    # 取走五份：显式指定唯一容器
    response = client.post("/api/forensics/withdrawals", headers=headers, json={
        "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
        "idempotency_key": "draw-W14-0001", "actor": "鉴定技术员", "reason": "实验室取走五份",
        "allocations": [{"placement_id": placement["id"], "quantity": 5}],
    })
    assert response.status_code == 201, response.text

    specimen_view = client.get(f"/api/forensics/specimens/{specimen['id']}", headers=headers).json()
    location_view = client.get(f"/api/forensics/locations/{location['id']}", headers=headers).json()
    reconcile_view = client.get(f"/api/forensics/specimens/{specimen['id']}/reconcile", headers=headers).json()

    assert specimen_view["available_quantity"] == 15
    assert specimen_view["placements"][0]["quantity"] == 15
    assert location_view["used_grams"] == 15
    assert reconcile_view["recorded_available_grams"] == 15
    assert reconcile_view["active_placement_grams"] == 15
    assert reconcile_view["conserved"] is True

    # 容器不足的请求整单回滚，三个视图仍是 15
    conflict = client.post("/api/forensics/withdrawals", headers=headers, json={
        "specimen_id": specimen["id"], "quantity": 99, "movement_type": "报废",
        "idempotency_key": "draw-W14-bad", "actor": "保管员", "reason": "超出剩余量",
    })
    assert conflict.status_code == 409, conflict.text
    specimen_view = client.get(f"/api/forensics/specimens/{specimen['id']}", headers=headers).json()
    location_view = client.get(f"/api/forensics/locations/{location['id']}", headers=headers).json()
    assert specimen_view["available_quantity"] == 15
    assert location_view["used_grams"] == 15

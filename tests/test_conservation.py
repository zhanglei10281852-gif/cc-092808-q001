from __future__ import annotations

import threading

import pytest

from app.core.errors import ConflictError
from app.database import get_connection, transaction
from app.forensics.service import ForensicService
from tests.test_forensics_workflow import create_accepted_forensic_case


def prepare_bag(service: ForensicService, suffix: str = "c01", container_total: int = 20) -> tuple[dict, dict, list[dict]]:
    """一袋检材分装到多个容器。"""
    forensic_case = create_accepted_forensic_case(service, suffix)
    location = service.custody.create_location({
        "location_code": f"VAULT-{suffix}", "facility": "检材保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    })
    specimen = service.custody.create_specimen({
        "specimen_no": f"SP-{suffix}", "case_id": forensic_case["id"], "parent_specimen_id": None,
        "received_year": 2026, "initial_quantity": container_total, "integrity_percent": 100,
        "packaging": "防拆封袋", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    placements = []
    first = service.custody.place_specimen({
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 12,
        "container_code": f"BOX-{suffix}-A", "idempotency_key": f"place-{suffix}-a", "actor": "保管员",
    })
    placements.append(first["placement"])
    second = service.custody.place_specimen({
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 8,
        "container_code": f"BOX-{suffix}-B", "idempotency_key": f"place-{suffix}-b", "actor": "保管员",
    })
    placements.append(second["placement"])
    return forensic_case, service.repository.specimen_detail(specimen["id"]), placements


def test_withdrawal_deducts_total_containers_and_location_together(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, placements = prepare_bag(service)
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
            "idempotency_key": "take-c01-0001", "actor": "技术员", "reason": "DNA 初检",
        })
        # 总量同步扣减
        assert result["specimen"]["available_quantity"] == 15
        # 稳定次序：先 BOX-C01-A（库位编码、容器编码、id 排序）
        items = result["movement"]["items"]
        assert len(items) == 1
        assert items[0]["placement_id"] == placements[0]["id"]
        assert items[0]["quantity"] == 5
        # 容器余量
        detail = service.repository.specimen_detail(specimen["id"])
        remaining = {p["id"]: p["remaining_quantity"] for p in detail["placements"]}
        assert remaining[placements[0]["id"]] == 7
        assert remaining[placements[1]["id"]] == 8
        # 库位占用 = 总账
        location = service.repository.location_detail(placements[0]["location_id"])
        assert location["used_grams"] == 15
        # 守恒
        report = service.custody.reconcile(specimen["id"])
        assert report["conservation_ok"] is True
        assert report["available_matches_ledger"] is True


def test_explicit_allocation_spans_multiple_containers(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, placements = prepare_bag(service, "c02")
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 15, "movement_type": "领用",
            "allocations": [
                {"placement_id": placements[0]["id"], "quantity": 10},
                {"placement_id": placements[1]["id"], "quantity": 5},
            ],
            "idempotency_key": "take-c02-0001", "actor": "保管员", "reason": "补充检验",
        })
        ids = {(item["placement_id"], item["quantity"]) for item in result["movement"]["items"]}
        assert ids == {(placements[0]["id"], 10), (placements[1]["id"], 5)}
        detail = service.repository.specimen_detail(specimen["id"])
        assert {p["remaining_quantity"] for p in detail["placements"]} == {2, 3}
        assert service.custody.reconcile(specimen["id"])["conservation_ok"] is True


def test_allocation_shortfall_and_removed_container_roll_back(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, placements = prepare_bag(service, "c03")
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 9, "movement_type": "领用",
                "allocations": [{"placement_id": placements[1]["id"], "quantity": 9}],
                "idempotency_key": "take-c03-bad1", "actor": "保管员", "reason": "超量",
            })
        # 容器已移出
        service.custody.move_placement(placements[1]["id"], {
            "target_location_id": service.custody.create_location({
                "location_code": "VAULT-C03-X", "facility": "检材保管室", "room": "冷藏区", "rack": "R2", "shelf": "S1",
                "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
            })["id"],
            "expected_version": 1, "idempotency_key": "move-c03-0001", "actor": "保管员", "reason": "移库",
        })
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 3, "movement_type": "取样",
                "allocations": [{"placement_id": placements[1]["id"], "quantity": 3}],
                "idempotency_key": "take-c03-bad2", "actor": "技术员", "reason": "追取",
            })
        # 整批回滚：总账与容器都不变
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 20
        assert service.custody.reconcile(specimen["id"])["conservation_ok"] is True


def test_stale_container_version_rolls_back(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, placements = prepare_bag(service, "c04")
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 2, "movement_type": "取样",
                "allocations": [{"placement_id": placements[0]["id"], "quantity": 2, "expected_version": 99}],
                "idempotency_key": "take-c04-bad", "actor": "技术员", "reason": "旧版本",
            })
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 20


def test_idempotent_replay_and_conflict(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, _ = prepare_bag(service, "c05")
        payload = {
            "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
            "idempotency_key": "take-c05-0001", "actor": "技术员", "reason": "初检",
        }
        first = service.custody.withdraw(payload)
        assert first["replayed"] is False
        second = service.custody.withdraw(payload)
        assert second["replayed"] is True
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 15
        with pytest.raises(ConflictError):
            service.custody.withdraw({**payload, "quantity": 6})
        with pytest.raises(ConflictError):
            service.custody.withdraw({**payload, "reason": "复检"})


def test_hold_is_not_bypassed_and_disposal_marks_disposed(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, _ = prepare_bag(service, "c06")
        hold = service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "保全", "reason": "争议待查", "actor": "审核员",
        })
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 1, "movement_type": "报废",
                "idempotency_key": "take-c06-bad", "actor": "保管员", "reason": "报废",
            })
        service.custody.release_hold(hold["id"], "审核员", "解除")
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 20, "movement_type": "报废",
            "idempotency_key": "take-c06-all", "actor": "保管员", "reason": "依法销毁",
        })
        assert result["specimen"]["status"] == "disposed"
        assert result["specimen"]["available_quantity"] == 0
        with pytest.raises(ConflictError):
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 1, "movement_type": "取样",
                "idempotency_key": "take-c06-after", "actor": "技术员", "reason": "再取",
            })


def test_concurrent_withdrawals_never_go_negative(client):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(key: str) -> None:
        from app.database import close_connection
        close_connection()
        barrier.wait(timeout=10)
        try:
            with transaction(immediate=True) as connection:
                service = ForensicService(connection)
                active = service.custody._active_placements(specimen_id)
                allocations = [{"placement_id": active[0]["id"], "quantity": 12.0}]
                service.custody.withdraw({
                    "specimen_id": specimen_id, "quantity": 12, "movement_type": "领用",
                    "allocations": allocations,
                    "idempotency_key": key, "actor": "保管员", "reason": "并发耗用",
                })
            outcomes.append("ok")
        except ConflictError:
            outcomes.append("conflict")
        except Exception as exc:  # pragma: no cover
            outcomes.append(f"error:{exc}")

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, _ = prepare_bag(service, "c07", container_total=20)
        specimen_id = specimen["id"]
    threads = [threading.Thread(target=worker, args=(f"take-c07-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes).count("ok") == 1
    assert "conflict" in outcomes
    close_connection = __import__("app.database", fromlist=["close_connection"]).close_connection
    close_connection()
    connection = get_connection()
    service = ForensicService(connection)
    detail = service.repository.specimen_detail(specimen_id)
    assert detail["available_quantity"] == 8
    assert service.custody.reconcile(specimen_id)["conservation_ok"] is True


def test_release_fulfillment_deducts_containers_and_is_replayable(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, specimen, placements = prepare_bag(service, "c08")
        request = service.release.create_request({
            "request_no": "REL-C08", "requester": "法医物证实验室", "purpose": "补充检验",
            "items": [{"case_id": forensic_case["id"], "quantity": 20}],
        })
        submitted = service.release.submit(request["id"], 1)
        approved = service.release.decide(request["id"], {
            "approve": True, "expected_version": submitted["version"], "actor": "审核员", "reason": "批准",
        })
        fulfilled = service.release.fulfill(request["id"], {
            "expected_version": approved["version"], "actor": "保管员", "allocations": {},
        })
        assert fulfilled["request"]["status"] == "fulfilled"
        movements = fulfilled["movements"]
        assert sum(item["quantity"] for movement in movements for item in movement["items"]) == 20
        detail = service.repository.specimen_detail(specimen["id"])
        assert detail["available_quantity"] == 0
        assert service.custody.reconcile(specimen["id"])["conservation_ok"] is True
        # 重复发放是重放而不是再次扣减
        replay = service.release.fulfill(request["id"], {
            "expected_version": 99, "actor": "保管员", "allocations": {},
        })
        assert replay.get("replayed") is True
        assert service.repository.specimen_detail(specimen["id"])["available_quantity"] == 0

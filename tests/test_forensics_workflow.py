from __future__ import annotations

from datetime import date

from app.database import get_connection, transaction
from app.forensics.service import ForensicService


def create_accepted_forensic_case(service: ForensicService, suffix: str = "001") -> dict:
    source = service.forensic_cases.create_agency({
        "agency_code": f"ORG-{suffix}", "agency_name": "市公安局物证中心", "jurisdiction_code": "CN",
        "contact_address": "政务区司法路 8 号", "licensed_on": "2025-10-02", "accreditation_no": "司鉴委-88",
        "restrictions": {},
    })
    forensic_case = service.forensic_cases.create_forensic_case({
        "case_no": f"CASE-{suffix}", "case_name": "身份关系鉴定", "discipline": "法医物证",
        "entrusted_matter": "亲缘关系鉴定", "agency_id": source["id"], "case_source": "委托",
        "accepted_on": "2026-09-01", "passport": {"commission_document": f"DOC-{suffix}"}, "created_by": "登记员",
    })
    return service.forensic_cases.transition(forensic_case["id"], {
        "target_status": "accepted", "reason": "资料与保全证明齐全", "expected_version": 1, "actor": "审核员",
    })


def create_stored_lot(service: ForensicService, suffix: str = "001") -> tuple[dict, dict, dict]:
    forensic_case = create_accepted_forensic_case(service, suffix)
    location = service.custody.create_location({
        "location_code": f"VAULT-{suffix}", "facility": "检材保管室", "room": "冷藏区", "rack": "R1", "shelf": "S1",
        "capacity_units": 1000, "reference_value": 4, "humidity_percent": 45,
    })
    specimen = service.custody.create_specimen({
        "specimen_no": f"SP-{suffix}", "case_id": forensic_case["id"], "parent_specimen_id": None,
        "received_year": 2026, "initial_quantity": 500, "integrity_percent": 100,
        "packaging": "防拆封袋，封识完整", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    placed = service.custody.place_specimen({
        "specimen_id": specimen["id"], "location_id": location["id"], "quantity": 500,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    return forensic_case, service.repository.specimen_detail(specimen["id"]), placed["placement"]


def test_forensic_case_intake_and_version_conflict(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case = create_accepted_forensic_case(service)
        assert forensic_case["status"] == "accepted"
        assert [item["event_type"] for item in forensic_case["events"]] == ["created", "status_changed"]
        try:
            service.forensic_cases.update_forensic_case(forensic_case["id"], {
                "discipline": "法医毒物", "expected_version": 1, "actor": "登记员",
            })
        except ConflictError as exc:
            assert exc.context["current_version"] == 2
        else:
            raise AssertionError("旧版本更新应被拒绝")


def test_custody_idempotency_and_capacity(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, placement = create_stored_lot(service)
        replay = service.custody.place_specimen({
            "specimen_id": specimen["id"], "location_id": placement["location_id"], "quantity": 500,
            "container_code": "BOX-001", "idempotency_key": "place-001-0001", "actor": "保管员",
        })
        assert replay["replayed"] is True
        too_small = service.custody.create_location({
            "location_code": "SMALL-001", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_units": 100, "reference_value": -18, "humidity_percent": 30,
        })
        try:
            service.custody.move_placement(placement["id"], {
                "target_location_id": too_small["id"], "expected_version": 1,
                "idempotency_key": "move-001-0001", "actor": "保管员", "reason": "库位整理",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("容量不足的移库应被拒绝")


def test_hold_blocks_withdrawal_until_release(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, _ = create_stored_lot(service)
        hold = service.custody.impose_hold({
            "specimen_id": specimen["id"], "hold_type": "质量", "reason": "等待复核", "actor": "审核员",
        })
        try:
            service.custody.withdraw({
                "specimen_id": specimen["id"], "quantity": 10, "movement_type": "领用",
                "idempotency_key": "withdraw-001-a", "actor": "保管员", "reason": "试验",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结检材不应允许领用")
        service.custody.release_hold(hold["id"], "审核员", "复核通过")
        result = service.custody.withdraw({
            "specimen_id": specimen["id"], "quantity": 10, "movement_type": "领用",
            "idempotency_key": "withdraw-001-b", "actor": "保管员", "reason": "试验",
        })
        assert result["specimen"]["available_quantity"] == 490


def test_examination_completion_creates_schedule(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        _, specimen, _ = create_stored_lot(service)
        protocol = service.examinations.create_protocol({
            "protocol_code": "DNA-REVIEW", "discipline": "法医物证", "observation_target": 100, "checkpoint_count": 2,
            "reference_value": 0.99, "turnaround_days": 14, "conclusion_rule": "位点质量满足复核阈值",
            "created_by": "技术负责人",
        })
        service.examinations.create_policy({
            "discipline": "法医物证", "risk_level": "medium", "interval_months": 12, "warning_days": 30,
            "minimum_conformity_percent": 75, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "技术负责人",
        })
        test = service.examinations.schedule_examination({
            "examination_no": "EX-001", "specimen_id": specimen["id"], "protocol_id": protocol["id"], "examination_type": "补充检验",
            "sample_quantity": 5, "scheduled_for": "2026-09-25", "requested_by": "检验员",
            "idempotency_key": "schedule-vt-001",
        })
        running = service.examinations.start_examination(test["id"], {"performed_by": "检验员", "expected_version": 1})
        assert running["status"] == "running"
        for replicate, normal in [(1, 80), (2, 82)]:
            service.examinations.add_observation(test["id"], {
                "checkpoint_no": replicate, "items_checked": 100, "conforming_count": normal,
                "exception_count": 10, "unusable_count": 100 - normal - 10, "pending_count": 0,
                "sequence_no": 14, "observed_by": "检验员",
            })
        completed = service.examinations.complete_examination(test["id"], {"performed_by": "检验员", "expected_version": 2})
        assert completed["conformity_percent"] == 81
        due = service.examinations.due_schedules(date(2028, 1, 1))
        assert len(due) == 1
        assert due[0]["due_on"].startswith("2027-")


def test_environment_reading_is_idempotent_and_alerts(client):
    from datetime import UTC, datetime

    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        location = service.custody.create_location({
            "location_code": "ENV-001", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
            "capacity_units": 1000, "reference_value": -18, "humidity_percent": 30,
        })
        payload = {
            "location_id": location["id"], "observed_at": datetime(2026, 9, 25, 8, 0, tzinfo=UTC),
            "reference_value": -5, "humidity_percent": 31, "source_key": "sensor-001-0800",
        }
        first = service.quality.add_reading(payload)
        second = service.quality.add_reading(payload)
        assert first["replayed"] is False and len(first["alerts"]) == 1
        assert second["replayed"] is True


def test_release_approval_allocates_eligible_lot(client):
    with transaction(immediate=True) as connection:
        service = ForensicService(connection)
        forensic_case, _, _ = create_stored_lot(service)
        request = service.release.create_request({
            "request_no": "REL-001", "requester": "法医物证实验室", "purpose": "补充检验取样",
            "items": [{"case_id": forensic_case["id"], "quantity": 20}],
        })
        submitted = service.release.submit(request["id"], 1)
        approved = service.release.decide(request["id"], {
            "approve": True, "expected_version": submitted["version"], "actor": "案件审核员", "reason": "材料充足",
        })
        assert approved["status"] == "approved"
        assert approved["items"][0]["allocated_specimen_id"] is not None

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record


ALLOWED_TRANSITIONS = {
    "draft": {"quarantine", "accepted", "retired"},
    "quarantine": {"accepted", "restricted", "retired"},
    "accepted": {"restricted", "retired"},
    "restricted": {"accepted", "retired"},
    "retired": set(),
}


class ForensicCaseService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def create_agency(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO submitting_agencies(agency_code,agency_name,jurisdiction_code,contact_address,licensed_on,"
                "accreditation_no,contact_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    data["agency_code"], data["agency_name"], data["jurisdiction_code"], data.get("contact_address", ""),
                    data.get("licensed_on"), data.get("accreditation_no"),
                    json.dumps(data.get("restrictions", {}), ensure_ascii=False, sort_keys=True), timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("委托机构编码已经存在") from exc
        return self.repository.require_agency(int(cursor.lastrowid))

    def update_agency_rules(self, agency_id: int, restrictions: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_agency(agency_id)
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE submitting_agencies SET contact_json=?,updated_at=? WHERE id=?",
            (json.dumps(restrictions, ensure_ascii=False, sort_keys=True), timestamp, agency_id),
        )
        after = self.repository.require_agency(agency_id)
        self._outbox(
            f"source-restrictions-{agency_id}-{timestamp}", "source.restrictions.changed", "source", agency_id,
            {"before": before.get("restrictions", {}), "after": after.get("restrictions", {})}, timestamp,
        )
        return after

    def create_forensic_case(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("agency_id"):
            self.repository.require_agency(int(data["agency_id"]))
        if self.repository.forensic_case_by_number(data["case_no"]):
            raise ConflictError("鉴定案件编号已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO forensic_cases(case_no,case_name,discipline,entrusted_matter,agency_id,case_source,"
            "accepted_on,status,case_profile_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?)",
            (
                data["case_no"], data["case_name"], data["discipline"], data.get("entrusted_matter", ""),
                data.get("agency_id"), data["case_source"], data["accepted_on"],
                json.dumps(data.get("passport", {}), ensure_ascii=False, sort_keys=True), data["created_by"],
                timestamp, timestamp,
            ),
        )
        case_id = int(cursor.lastrowid)
        self._event(case_id, "created", data["created_by"], None, "draft", {"number": data["case_no"]})
        self._outbox(
            f"forensic_case-created-{case_id}", "forensic_case.created", "forensic_case", case_id,
            {"case_no": data["case_no"], "discipline": data["discipline"]}, timestamp,
        )
        return self.repository.forensic_case_detail(case_id)

    def update_forensic_case(self, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_forensic_case(case_id)
        if int(before["version"]) != int(data["expected_version"]):
            raise ConflictError("鉴定案件已被其他人修改", context={"current_version": before["version"]})
        if before["status"] == "retired":
            raise ConflictError("已退出保存的案件不能修改")
        allowed = {key: value for key, value in data.items() if key in {
            "case_name", "discipline", "entrusted_matter", "agency_id", "passport"
        } and value is not None}
        if not allowed:
            raise ValidationError("没有可更新的案件字段")
        if "agency_id" in allowed:
            self.repository.require_agency(int(allowed["agency_id"]))
        columns: list[str] = []
        params: list[Any] = []
        for key, value in allowed.items():
            if key == "passport":
                columns.append("case_profile_json=?")
                params.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            else:
                columns.append(f"{key}=?")
                params.append(value)
        timestamp = to_storage(self.clock.now())
        params.extend([timestamp, case_id, data["expected_version"]])
        cursor = self.connection.execute(
            f"UPDATE forensic_cases SET {','.join(columns)},version=version+1,updated_at=? WHERE id=? AND version=?",
            params,
        )
        if cursor.rowcount != 1:
            raise ConflictError("鉴定案件版本冲突")
        after = self.repository.require_forensic_case(case_id)
        self._event(case_id, "updated", data["actor"], before["status"], after["status"], {
            "changed_fields": sorted(allowed), "before_version": before["version"], "after_version": after["version"]
        })
        return self.repository.forensic_case_detail(case_id)

    def transition(self, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        before = self.repository.require_forensic_case(case_id)
        current = str(before["status"])
        target = str(data["target_status"])
        if int(before["version"]) != int(data["expected_version"]):
            raise ConflictError("鉴定案件状态版本冲突", context={"current_version": before["version"]})
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise ConflictError("不允许执行该案件状态转换", context={"from": current, "to": target})
        reason = data.get("reason", "").strip()
        if target in {"quarantine", "restricted", "retired"} and not reason:
            raise ValidationError("隔离、限制或退出保存时必须填写原因")
        if target == "accepted" and before.get("agency_id") is None:
            raise ValidationError("正式接收前必须登记来源信息")
        if target == "accepted" and not before.get("case_name"):
            raise ValidationError("正式接收前必须登记学名")
        timestamp = to_storage(self.clock.now())
        return_reason = reason if target in {"quarantine", "restricted"} else ""
        cursor = self.connection.execute(
            "UPDATE forensic_cases SET status=?,return_reason=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (target, return_reason, timestamp, case_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("鉴定案件状态版本冲突")
        self._event(case_id, "status_changed", data["actor"], current, target, {"reason": reason})
        self._outbox(
            f"forensic_case-status-{case_id}-{int(before['version']) + 1}", "forensic_case.status.changed",
            "forensic_case", case_id, {"from": current, "to": target, "reason": reason}, timestamp,
        )
        return self.repository.forensic_case_detail(case_id)

    def restrictions_for(self, case_id: int) -> dict[str, Any]:
        forensic_case = self.repository.require_forensic_case(case_id)
        source_rules: dict[str, Any] = {}
        if forensic_case.get("agency_id"):
            source_rules = self.repository.require_agency(int(forensic_case["agency_id"])).get("restrictions", {})
        passport_rules = forensic_case.get("passport", {}).get("restrictions", {})
        return {
            "case_id": case_id,
            "status": forensic_case["status"],
            "source": source_rules,
            "passport": passport_rules,
            "release_allowed": (
                forensic_case["status"] == "accepted"
                and not source_rules.get("no_release", False)
                and not passport_rules.get("no_release", False)
            ),
        }

    def _event(
        self,
        case_id: int,
        event_type: str,
        actor: str,
        from_status: str | None,
        to_status: str | None,
        detail: dict[str, Any],
    ) -> None:
        self.connection.execute(
            "INSERT INTO case_events(case_id,event_type,actor,from_status,to_status,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                case_id, event_type, actor, from_status, to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True), to_storage(self.clock.now()),
            ),
        )

    def _outbox(
        self,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: int,
        payload: dict[str, Any],
        timestamp: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO outbox_events(event_key,event_type,aggregate_type,aggregate_id,payload_json,available_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (event_key, event_type, aggregate_type, str(aggregate_id), json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
        )

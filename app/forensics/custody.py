from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record


def item_code(active: list[dict[str, Any]], placement_id: int) -> str:
    return next(item["container_code"] for item in active if int(item["id"]) == placement_id)


def item_version(active: list[dict[str, Any]], placement_id: int) -> int:
    return int(next(item["version"] for item in active if int(item["id"]) == placement_id))


class CustodyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = ForensicRepository(connection)

    def create_location(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO storage_locations(location_code,facility,room,rack,shelf,capacity_units,reference_value,"
                "humidity_percent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    data["location_code"], data["facility"], data["room"], data["rack"], data["shelf"],
                    data["capacity_units"], data["reference_value"], data["humidity_percent"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("库位编码已经存在") from exc
        return self.repository.location_detail(int(cursor.lastrowid))

    def change_location_status(self, location_id: int, status: str, expected_version: int) -> dict[str, Any]:
        if status not in {"active", "maintenance", "closed"}:
            raise ValidationError("库位状态无效")
        before = self.repository.require_location(location_id)
        if int(before["version"]) != expected_version:
            raise ConflictError("库位版本冲突", context={"current_version": before["version"]})
        if status == "closed" and self.repository.location_usage(location_id) > 0:
            raise ConflictError("库位中仍有检材容器，不能关闭")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE storage_locations SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (status, timestamp, location_id, expected_version),
        )
        return self.repository.location_detail(location_id)

    def create_specimen(self, data: dict[str, Any]) -> dict[str, Any]:
        forensic_case = self.repository.require_forensic_case(int(data["case_id"]))
        if forensic_case["status"] not in {"accepted", "restricted", "quarantine"}:
            raise ConflictError("案件尚未受理，不能登记检材")
        parent = None
        if data.get("parent_specimen_id"):
            parent = self.repository.require_specimen(int(data["parent_specimen_id"]))
            if int(parent["case_id"]) != int(data["case_id"]):
                raise ValidationError("子检材必须与来源检材属于同一案件")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO specimens(specimen_no,case_id,parent_specimen_id,received_year,initial_quantity,"
                "available_quantity,integrity_percent,packaging,sealed_on,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    data["specimen_no"], data["case_id"], data.get("parent_specimen_id"), data["received_year"],
                    data["initial_quantity"], data["initial_quantity"], data.get("integrity_percent"),
                    data.get("packaging", ""), data.get("sealed_on"), data["created_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("检材编号已经存在") from exc
        specimen_id = int(cursor.lastrowid)
        if parent:
            self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                "VALUES(?,'盘点调整',0,?,?,?,?)",
                (specimen_id, f"lineage-{specimen_id}", data["created_by"], f"由来源检材 {parent['specimen_no']} 分取", timestamp),
            )
        return self.repository.specimen_detail(specimen_id)

    def place_specimen(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.custody_event_by_key(data["idempotency_key"])
        if previous:
            placement = self.repository.require_placement(int(previous["placement_id"]))
            return {"placement": placement, "replayed": True}
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        location = self.repository.require_location(int(data["location_id"]))
        if specimen["status"] in {"depleted", "disposed"}:
            raise ConflictError("检材已经耗尽或销毁")
        if location["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        active_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(p.quantity - "
            "(SELECT COALESCE(SUM(i.quantity),0) FROM custody_event_items i WHERE i.placement_id=p.id)),0) "
            "FROM specimen_placements p WHERE p.specimen_id=? AND p.removed_at IS NULL",
            (specimen["id"],),
        ).fetchone()[0])
        if active_weight + float(data["quantity"]) > float(specimen["available_quantity"]) + 1e-9:
            raise ValidationError("摆放数量超过检材可用数量")
        used = self.repository.location_usage(int(location["id"]))
        if used + float(data["quantity"]) > float(location["capacity_units"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": location["capacity_units"] - used})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
                (specimen["id"], location["id"], data["quantity"], data["container_code"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("容器编码与入库时间冲突") from exc
        placement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO custody_events(specimen_id,placement_id,movement_type,quantity,to_location_id,idempotency_key,"
            "actor,reason,created_at) VALUES(?,?,'入库',?,?,?,?,?,?)",
            (specimen["id"], placement_id, data["quantity"], location["id"], data["idempotency_key"], data["actor"], "首次入库", timestamp),
        )
        self.connection.execute(
            "UPDATE specimens SET status='stored',version=version+1,updated_at=? WHERE id=?",
            (timestamp, specimen["id"]),
        )
        return {"placement": self.repository.require_placement(placement_id), "replayed": False}

    def move_placement(self, placement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.custody_event_by_key(data["idempotency_key"])
        if previous:
            return {"placement": self.repository.require_placement(int(previous["placement_id"])), "replayed": True}
        placement = self.repository.require_placement(placement_id)
        if placement["removed_at"]:
            raise ConflictError("容器已经移出原库位")
        if int(placement["version"]) != int(data["expected_version"]):
            raise ConflictError("容器摆放版本冲突", context={"current_version": placement["version"]})
        target = self.repository.require_location(int(data["target_location_id"]))
        if target["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        remaining_quantity = self.repository.placement_remaining(placement_id)
        if remaining_quantity <= 1e-9:
            raise ConflictError("容器内检材已经耗尽，不能移库")
        used = self.repository.location_usage(int(target["id"]))
        if used + remaining_quantity > float(target["capacity_units"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": target["capacity_units"] - used})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
                (placement["specimen_id"], target["id"], remaining_quantity, placement["container_code"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            # 时间戳按秒存储，同一秒内连续移库同一容器会与既有行撞唯一键，顺延一秒即可。
            from datetime import timedelta

            from app.core.clock import from_storage

            bumped = to_storage(from_storage(timestamp) + timedelta(seconds=1))
            try:
                cursor = self.connection.execute(
                    "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
                    (placement["specimen_id"], target["id"], remaining_quantity, placement["container_code"], bumped),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("容器移库记录冲突，请稍后重试") from exc
            timestamp = bumped
        new_id = int(cursor.lastrowid)
        updated = self.connection.execute(
            "UPDATE specimen_placements SET removed_at=?,version=version+1 WHERE id=? AND version=? AND removed_at IS NULL",
            (timestamp, placement_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("容器摆放版本冲突")
        self.connection.execute(
            "INSERT INTO custody_events(specimen_id,placement_id,movement_type,quantity,from_location_id,to_location_id,"
            "idempotency_key,actor,reason,created_at) VALUES(?,?,'移库',?,?,?,?,?,?,?)",
            (
                placement["specimen_id"], new_id, remaining_quantity, placement["location_id"], target["id"],
                data["idempotency_key"], data["actor"], data["reason"], timestamp,
            ),
        )
        return {"placement": self.repository.require_placement(new_id), "replayed": False}

    @staticmethod
    def _withdrawal_fingerprint(data: dict[str, Any]) -> str:
        payload = {
            "specimen_id": int(data["specimen_id"]),
            "quantity": round(float(data["quantity"]), 6),
            "movement_type": data["movement_type"],
            "reason": data["reason"],
            "allocations": [
                {
                    "placement_id": int(item.get("placement_id", 0)),
                    "quantity": round(float(item.get("quantity", 0)), 6),
                }
                for item in data.get("allocations") or []
            ],
        }
        compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(compact.encode()).hexdigest()

    def _active_placements(self, specimen_id: int) -> list[dict[str, Any]]:
        """稳定且可解释的默认扣减次序：库位编码、容器编码、摆放记录编号。"""
        rows = self.connection.execute(
            "SELECT p.*,s.location_code FROM specimen_placements p JOIN storage_locations s ON s.id=p.location_id "
            "WHERE p.specimen_id=? AND p.removed_at IS NULL ORDER BY s.location_code,p.container_code,p.id",
            (specimen_id,),
        ).fetchall()
        return [record(row) or {} for row in rows]

    def _resolve_allocation(self, specimen: dict[str, Any], data: dict[str, Any]) -> list[dict[str, Any]]:
        quantity = round(float(data["quantity"]), 6)
        if quantity <= 0:
            raise ValidationError("耗用数量必须为正数")
        active = self._active_placements(int(specimen["id"]))
        remaining = {
            int(item["id"]): round(float(item["quantity"]) - self.repository.placement_consumed(int(item["id"])), 6)
            for item in active
        }
        requested = data.get("allocations")
        allocations: list[dict[str, Any]] = []
        if requested:
            seen: set[int] = set()
            for raw in requested:
                placement_id = int(raw.get("placement_id", 0))
                share = round(float(raw.get("quantity", 0)), 6)
                if placement_id <= 0 or share <= 0:
                    raise ValidationError("扣减分配必须包含有效的容器与正数数量")
                if placement_id in seen:
                    raise ValidationError("同一容器不能在分配中重复出现", context={"placement_id": placement_id})
                seen.add(placement_id)
                if placement_id not in remaining:
                    raise ConflictError("容器不属于该检材、已经移出或耗尽", context={"placement_id": placement_id})
                expected_version = raw.get("expected_version")
                if expected_version is not None and int(item_version(active, placement_id)) != int(expected_version):
                    raise ConflictError("容器版本过期", context={
                        "placement_id": placement_id,
                        "current_version": item_version(active, placement_id),
                    })
                if share > remaining[placement_id] + 1e-9:
                    raise ConflictError("容器内检材数量不足", context={
                        "placement_id": placement_id,
                        "container_code": item_code(active, placement_id),
                        "remaining_quantity": remaining[placement_id],
                        "requested_quantity": share,
                    })
                allocations.append({
                    "placement_id": placement_id,
                    "quantity": share,
                    "container_code": item_code(active, placement_id),
                    "expected_version": int(expected_version) if expected_version is not None else None,
                })
        else:
            pending = quantity
            for item in active:
                available = remaining[int(item["id"])]
                if available <= 1e-9:
                    continue
                share = round(min(available, pending), 6)
                allocations.append({
                    "placement_id": int(item["id"]),
                    "quantity": share,
                    "container_code": item["container_code"],
                    "expected_version": None,
                })
                pending = round(pending - share, 6)
                if pending <= 1e-9:
                    break
            if pending > 1e-9:
                raise ConflictError("检材在库容器数量不足", context={
                    "requested_quantity": quantity,
                    "available_in_placements": round(quantity - pending, 6),
                })
        total = round(sum(item["quantity"] for item in allocations), 6)
        if abs(total - quantity) > 1e-6:
            raise ValidationError("各容器扣减数量之和必须等于本次耗用数量", context={
                "requested_quantity": quantity,
                "allocated_quantity": total,
            })
        return allocations

    def _movement_with_items(self, event_id: int) -> dict[str, Any]:
        movement = record(self.connection.execute(
            "SELECT * FROM custody_events WHERE id=?", (event_id,)
        ).fetchone()) or {}
        movement["items"] = self.repository.custody_event_items(event_id)
        return movement

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        key = data["idempotency_key"]
        fingerprint = self._withdrawal_fingerprint(data)
        previous = self.repository.custody_event_by_key(key)
        if previous:
            if previous["request_hash"] != fingerprint:
                raise ConflictError("同一业务键携带了不同的耗用数量、用途或容器分配")
            return {
                "specimen": self.repository.specimen_detail(int(previous["specimen_id"])),
                "movement": self._movement_with_items(int(previous["id"])),
                "replayed": True,
            }
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        if specimen["status"] == "disposed":
            raise ConflictError("检材已经销毁，不能再耗用")
        holds = self.repository.active_holds(int(specimen["id"]))
        if holds:
            raise ConflictError("检材存在未解除的保全、质量或权限冻结", context={"holds": [item["id"] for item in holds]})
        quantity = round(float(data["quantity"]), 6)
        if quantity > float(specimen["available_quantity"]) + 1e-9:
            raise ConflictError("检材可用数量不足")
        allocations = self._resolve_allocation(specimen, data)
        timestamp = to_storage(self.clock.now())
        remaining_quantity = round(float(specimen["available_quantity"]) - quantity, 6)
        if remaining_quantity <= 1e-9:
            new_status = "disposed" if data["movement_type"] == "报废" else "depleted"
        else:
            new_status = specimen["status"]
        try:
            updated = self.connection.execute(
                "UPDATE specimens SET available_quantity=?,status=?,version=version+1,updated_at=? "
                "WHERE id=? AND version=? AND available_quantity>=?",
                (remaining_quantity, new_status, timestamp, specimen["id"], specimen["version"], quantity),
            )
            if updated.rowcount != 1:
                raise ConflictError("检材版本冲突或可用数量已变化，请刷新后重试")
            cursor = self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,request_hash,"
                "actor,reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    specimen["id"], data["movement_type"], -quantity, key, fingerprint,
                    data["actor"], data["reason"], timestamp,
                ),
            )
            event_id = int(cursor.lastrowid)
            for item in allocations:
                self.connection.execute(
                    "INSERT INTO custody_event_items(custody_event_id,placement_id,quantity,container_code,consumed_at) "
                    "VALUES(?,?,?,?,?)",
                    (event_id, item["placement_id"], item["quantity"], item["container_code"], timestamp),
                )
                guard = (
                    "UPDATE specimen_placements SET version=version+1 "
                    "WHERE id=? AND removed_at IS NULL "
                    "AND quantity - (SELECT COALESCE(SUM(i.quantity),0) FROM custody_event_items i WHERE i.placement_id=?) >= -1e-9"
                )
                params: list[Any] = [item["placement_id"], item["placement_id"]]
                if item["expected_version"] is not None:
                    guard += " AND version=?"
                    params.append(item["expected_version"])
                bumped = self.connection.execute(guard, params)
                if bumped.rowcount != 1:
                    raise ConflictError("容器不足、已移出或版本过期，整次耗用回滚", context={
                        "placement_id": item["placement_id"],
                        "container_code": item["container_code"],
                    })
        except sqlite3.IntegrityError as exc:
            raise ConflictError("同一业务键的耗用正在处理或已完成") from exc
        return {
            "specimen": self.repository.specimen_detail(int(specimen["id"])),
            "movement": self._movement_with_items(event_id),
            "replayed": False,
        }

    def impose_hold(self, data: dict[str, Any]) -> dict[str, Any]:
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        existing = self.connection.execute(
            "SELECT * FROM specimen_holds WHERE specimen_id=? AND hold_type=? AND released_at IS NULL",
            (specimen["id"], data["hold_type"]),
        ).fetchone()
        if existing:
            raise ConflictError("该类型冻结已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO specimen_holds(specimen_id,hold_type,reason,imposed_by,imposed_at) VALUES(?,?,?,?,?)",
            (specimen["id"], data["hold_type"], data["reason"], data["actor"], timestamp),
        )
        self.connection.execute(
            "UPDATE specimens SET status='held',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('depleted','disposed')",
            (timestamp, specimen["id"]),
        )
        return record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def release_hold(self, hold_id: int, actor: str, reason: str) -> dict[str, Any]:
        hold = record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (hold_id,)).fetchone())
        if not hold:
            raise ValidationError("冻结记录不存在")
        if hold["released_at"]:
            raise ConflictError("冻结记录已经解除")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE specimen_holds SET released_by=?,released_at=?,release_reason=? WHERE id=? AND released_at IS NULL",
            (actor, timestamp, reason, hold_id),
        )
        remaining = self.repository.active_holds(int(hold["specimen_id"]))
        if not remaining:
            self.connection.execute(
                "UPDATE specimens SET status=CASE WHEN available_quantity<=0 THEN 'depleted' ELSE 'stored' END,"
                "version=version+1,updated_at=? WHERE id=? AND status='held'",
                (timestamp, hold["specimen_id"]),
            )
        return record(self.connection.execute("SELECT * FROM specimen_holds WHERE id=?", (hold_id,)).fetchone()) or {}

    def reconcile(self, specimen_id: int) -> dict[str, Any]:
        specimen = self.repository.require_specimen(specimen_id)
        movement_total = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM custody_events WHERE specimen_id=? AND movement_type IN ('取样','领用','报废','归还','盘点调整')",
            (specimen_id,),
        ).fetchone()[0])
        expected_available = round(float(specimen["initial_quantity"]) + movement_total, 6)
        consumed_grams = float(self.connection.execute(
            "SELECT COALESCE(SUM(ce.quantity),0) FROM custody_events ce "
            "WHERE ce.specimen_id=? AND ce.movement_type IN ('取样','领用','报废')",
            (specimen_id,),
        ).fetchone()[0])
        consumed_grams = round(-consumed_grams, 6)
        container_ledger_grams = float(self.connection.execute(
            "SELECT COALESCE(SUM(i.quantity),0) FROM custody_event_items i "
            "JOIN custody_events ce ON ce.id=i.custody_event_id WHERE ce.specimen_id=?",
            (specimen_id,),
        ).fetchone()[0])
        placed_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(p.quantity - "
            "(SELECT COALESCE(SUM(i.quantity),0) FROM custody_event_items i WHERE i.placement_id=p.id)),0) "
            "FROM specimen_placements p WHERE p.specimen_id=? AND p.removed_at IS NULL",
            (specimen_id,),
        ).fetchone()[0])
        overdrawn_containers = int(self.connection.execute(
            "SELECT COUNT(*) FROM specimen_placements p WHERE p.specimen_id=? AND p.removed_at IS NULL AND "
            "p.quantity - (SELECT COALESCE(SUM(i.quantity),0) FROM custody_event_items i WHERE i.placement_id=p.id) < -1e-6",
            (specimen_id,),
        ).fetchone()[0])
        recorded = float(specimen["available_quantity"])
        ledger_matches = abs(recorded - expected_available) < 1e-6
        containers_traceable = abs(container_ledger_grams - consumed_grams) < 1e-6 and overdrawn_containers == 0
        placements_within_available = placed_weight <= recorded + 1e-6
        return {
            "specimen_id": specimen_id,
            "recorded_available_grams": recorded,
            "expected_available_grams": expected_available,
            "active_placement_grams": round(placed_weight, 6),
            "consumed_grams": consumed_grams,
            "container_ledger_grams": round(container_ledger_grams, 6),
            "available_matches_ledger": ledger_matches,
            "placements_within_available": placements_within_available,
            "containers_traceable": containers_traceable,
            "conservation_ok": ledger_matches and containers_traceable and placements_within_available,
        }

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, ValidationError
from app.forensics.repository import ForensicRepository, record

# 浮点数量统一保留 6 位小数，比较与条件更新都以此容差兜底
QUANTITY_EPSILON = 1e-9


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
            "SELECT COALESCE(SUM(quantity),0) FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL",
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
        used = self.repository.location_usage(int(target["id"]))
        if used + float(placement["quantity"]) > float(target["capacity_units"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": target["capacity_units"] - used})
        timestamp = to_storage(self.clock.now())
        # 时间戳精确到秒；同一秒内连续移库时，新摆放时间必须晚于该容器上次摆放时间以免唯一约束冲突
        latest_placed = self.connection.execute(
            "SELECT MAX(placed_at) FROM specimen_placements WHERE container_code=?",
            (placement["container_code"],),
        ).fetchone()[0]
        if latest_placed and timestamp <= latest_placed:
            timestamp = to_storage(from_storage(latest_placed) + timedelta(seconds=1))
        cursor = self.connection.execute(
            "INSERT INTO specimen_placements(specimen_id,location_id,quantity,container_code,placed_at) VALUES(?,?,?,?,?)",
            (placement["specimen_id"], target["id"], placement["quantity"], placement["container_code"], timestamp),
        )
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
                placement["specimen_id"], new_id, placement["quantity"], placement["location_id"], target["id"],
                data["idempotency_key"], data["actor"], data["reason"], timestamp,
            ),
        )
        return {"placement": self.repository.require_placement(new_id), "replayed": False}

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        key = data["idempotency_key"]
        quantity = round(float(data["quantity"]), 6)
        if quantity <= 0:
            raise ValidationError("耗用数量必须为正数")
        previous = self.repository.custody_event_by_key(key)
        # 命中相同业务键时优先做冲突比对：数量/类型/用途不同即冲突，不进入后续结构校验
        if previous:
            requested = self._normalize_allocations(data.get("allocations"), quantity, strict=False)
            return self._replay_withdrawal(previous, data, quantity, requested)
        requested = self._normalize_allocations(data.get("allocations"), quantity)
        specimen = self.repository.require_specimen(int(data["specimen_id"]))
        if specimen["status"] == "disposed":
            raise ConflictError("检材已经销毁，不能再耗用")
        holds = self.repository.active_holds(int(specimen["id"]))
        if holds:
            raise ConflictError("检材存在未解除的保全、质量或权限冻结", context={"holds": [item["id"] for item in holds]})
        if quantity > float(specimen["available_quantity"]) + QUANTITY_EPSILON:
            raise ConflictError("检材可用数量不足")
        allocations = self._resolve_allocations(specimen, requested, quantity)
        allocation_mode = "auto" if requested is None else "explicit"
        timestamp = to_storage(self.clock.now())
        # 检材总量条件扣减：available_quantity>=? 兜底，任何并发耗用都不会产生负数
        updated = self.connection.execute(
            "UPDATE specimens SET available_quantity=ROUND(available_quantity-?,6),"
            "status=CASE WHEN ROUND(available_quantity-?,6)<=0 "
            "THEN CASE ? WHEN '报废' THEN 'disposed' ELSE 'depleted' END ELSE status END,"
            "version=version+1,updated_at=? WHERE id=? AND available_quantity>=?",
            (quantity, quantity, data["movement_type"], timestamp, specimen["id"], quantity - QUANTITY_EPSILON),
        )
        if updated.rowcount != 1:
            raise ConflictError("检材可用数量不足或检材已被并发耗用")
        try:
            cursor = self.connection.execute(
                "INSERT INTO custody_events(specimen_id,movement_type,quantity,idempotency_key,actor,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (specimen["id"], data["movement_type"], -quantity, key, data["actor"], data["reason"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("相同业务键的耗用请求正在处理或已存在") from exc
        event_id = int(cursor.lastrowid)
        # 逐容器条件扣减；不足、已移出或版本过期都会令 rowcount=0 并回滚整次耗用
        for placement_id, container_code, amount, expected_version in allocations:
            result = self.connection.execute(
                "UPDATE specimen_placements SET quantity=ROUND(quantity-?,6),version=version+1 "
                "WHERE id=? AND specimen_id=? AND removed_at IS NULL AND quantity>=?"
                + (" AND version=?" if expected_version is not None else ""),
                (amount, placement_id, specimen["id"], amount - QUANTITY_EPSILON)
                + ((expected_version,) if expected_version is not None else ()),
            )
            if result.rowcount != 1:
                current = record(self.connection.execute(
                    "SELECT * FROM specimen_placements WHERE id=?", (placement_id,)
                ).fetchone())
                if current is None or int(current["specimen_id"]) != int(specimen["id"]):
                    raise ConflictError("扣减容器不存在或不属于该检材", context={"placement_id": placement_id})
                if current["removed_at"]:
                    raise ConflictError("容器已经移出原库位，不能再扣减", context={"placement_id": placement_id})
                if expected_version is not None and int(current["version"]) != expected_version:
                    raise ConflictError("容器摆放版本过期", context={
                        "placement_id": placement_id, "current_version": current["version"],
                    })
                raise ConflictError("容器内数量不足，整次耗用已回滚", context={
                    "placement_id": placement_id, "container_code": current["container_code"],
                    "available": current["quantity"], "requested": amount,
                })
            self.connection.execute(
                "INSERT INTO custody_consumptions(event_id,specimen_id,placement_id,container_code,quantity,"
                "placement_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_id, specimen["id"], placement_id, container_code, amount,
                    expected_version if expected_version is not None else 0,
                    json.dumps({
                        "mode": allocation_mode,
                        "allocation": {"placement_id": placement_id, "quantity": amount},
                    }, ensure_ascii=False),
                    timestamp,
                ),
            )
            # 容器被取空即视为该封装物理移出当前库位，不再占用容量；记录保留以供耗用追溯
            self.connection.execute(
                "UPDATE specimen_placements SET removed_at=? WHERE id=? AND removed_at IS NULL AND quantity<=?",
                (timestamp, placement_id, QUANTITY_EPSILON),
            )
        movement = record(self.connection.execute(
            "SELECT * FROM custody_events WHERE id=?", (event_id,)
        ).fetchone())
        return {
            "specimen": self.repository.specimen_detail(int(specimen["id"])),
            "movement": movement,
            "consumptions": self.repository.consumptions_for_event(event_id),
            "replayed": False,
        }

    def _normalize_allocations(
        self, raw: list[dict[str, Any]] | None, total: float, *, strict: bool = True
    ) -> list[dict[str, int | float | None]] | None:
        """校验显式分配：正数、容器不重复、合计必须等于请求总量。

        幂等重试路径传 strict=False：此时数量本身可能就是冲突点，
        合计校验会被 _replay_withdrawal 的数量比对取代。
        """
        if raw is None:
            return None
        if not raw:
            raise ValidationError("容器分配清单不能为空")
        normalized: list[dict[str, int | float | None]] = []
        seen: set[int] = set()
        subtotal = 0.0
        for item in raw:
            placement_id = int(item.get("placement_id", 0))
            amount = round(float(item.get("quantity", 0)), 6)
            if placement_id <= 0 or amount <= 0:
                raise ValidationError("容器分配必须包含有效的摆放记录和正数数量")
            if placement_id in seen:
                raise ValidationError("同一容器不能在分配清单中重复出现", context={"placement_id": placement_id})
            seen.add(placement_id)
            version = item.get("expected_version")
            normalized.append({
                "placement_id": placement_id,
                "quantity": amount,
                "expected_version": int(version) if version is not None else None,
            })
            subtotal = round(subtotal + amount, 6)
        if strict and abs(subtotal - total) > QUANTITY_EPSILON:
            raise ValidationError("各容器分配数量之和必须等于耗用总量", context={
                "allocated": subtotal, "requested": total,
            })
        return normalized

    def _resolve_allocations(
        self,
        specimen: dict[str, Any],
        requested: list[dict[str, int | float | None]] | None,
        total: float,
    ) -> list[tuple[int, str, float, int | None]]:
        """返回 (placement_id, container_code, amount, expected_version) 的稳定扣减序列。"""
        active = self.repository.active_placements(int(specimen["id"]))
        if requested is None:
            return self._auto_allocate(active, total)
        by_id = {int(item["id"]): item for item in active}
        resolved: list[tuple[int, str, float, int | None]] = []
        for item in requested:
            placement_id = int(item["placement_id"])
            placement = by_id.get(placement_id)
            if placement is None:
                stored = record(self.connection.execute(
                    "SELECT * FROM specimen_placements WHERE id=?", (placement_id,)
                ).fetchone())
                if stored is None or int(stored["specimen_id"]) != int(specimen["id"]):
                    raise ConflictError("扣减容器不存在或不属于该检材", context={"placement_id": placement_id})
                raise ConflictError("容器已经移出原库位，不能再扣减", context={"placement_id": placement_id})
            amount = float(item["quantity"])
            if amount > float(placement["quantity"]) + QUANTITY_EPSILON:
                raise ConflictError("指定容器内数量不足，整次耗用已回滚", context={
                    "placement_id": placement_id, "container_code": placement["container_code"],
                    "available": placement["quantity"], "requested": amount,
                })
            resolved.append((placement_id, placement["container_code"], amount, item["expected_version"]))
        return resolved

    def _auto_allocate(
        self, active: list[dict[str, Any]], total: float
    ) -> list[tuple[int, str, float, int | None]]:
        """未指定分配时按入库时间从早到晚依次扣减（先进先出），次序稳定可解释。"""
        if not active:
            raise ConflictError("检材没有在库容器，无法耗用")
        allocations: list[tuple[int, str, float, int | None]] = []
        remaining = total
        for placement in active:
            available = float(placement["quantity"])
            if available <= QUANTITY_EPSILON:
                continue
            amount = round(min(available, remaining), 6)
            allocations.append((int(placement["id"]), placement["container_code"], amount, None))
            remaining = round(remaining - amount, 6)
            if remaining <= QUANTITY_EPSILON:
                return allocations
        raise ConflictError("在库容器数量之和不足，整次耗用已回滚", context={
            "available_in_containers": round(total - remaining, 6), "requested": total,
        })

    def _replay_withdrawal(
        self,
        previous: dict[str, Any],
        data: dict[str, Any],
        quantity: float,
        requested: list[dict[str, int | float | None]] | None,
    ) -> dict[str, Any]:
        """相同业务键重试：内容一致则幂等回放，数量/用途/分配不同则报告冲突。"""
        if previous["movement_type"] != data["movement_type"]:
            raise ConflictError("同一业务键已用于不同耗用类型", context={
                "existing": previous["movement_type"], "requested": data["movement_type"],
            })
        if abs(abs(float(previous["quantity"])) - quantity) > QUANTITY_EPSILON:
            raise ConflictError("同一业务键对应了不同耗用数量", context={
                "existing": abs(float(previous["quantity"])), "requested": quantity,
            })
        if previous["reason"] != data["reason"]:
            raise ConflictError("同一业务键对应了不同用途说明", context={
                "existing": previous["reason"], "requested": data["reason"],
            })
        existing = self.repository.consumptions_for_event(int(previous["id"]))
        existing_map = {int(item["placement_id"]): round(float(item["quantity"]), 6) for item in existing}
        original_explicit = any((item.get("payload") or {}).get("mode") == "explicit" for item in existing)
        if requested is None:
            if original_explicit:
                raise ConflictError("同一业务键对应了不同的容器扣减分配", context={
                    "existing": [{"placement_id": pid, "quantity": amount} for pid, amount in sorted(existing_map.items())],
                    "requested": "auto",
                })
            requested_map = dict(existing_map)
        else:
            requested_map = {int(item["placement_id"]): round(float(item["quantity"]), 6) for item in requested}
        if existing_map != requested_map:
            raise ConflictError("同一业务键对应了不同的容器扣减分配", context={
                "existing": [{"placement_id": pid, "quantity": amount} for pid, amount in sorted(existing_map.items())],
                "requested": [{"placement_id": pid, "quantity": amount} for pid, amount in sorted(requested_map.items())],
            })
        return {
            "specimen": self.repository.specimen_detail(int(previous["specimen_id"])),
            "movement": previous,
            "consumptions": existing,
            "replayed": True,
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
        placed_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM specimen_placements WHERE specimen_id=? AND removed_at IS NULL", (specimen_id,)
        ).fetchone()[0])
        # 容器级耗用明细之和必须与流水（负数量）及检材总账完全对齐
        consumed_detail = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM custody_consumptions WHERE specimen_id=?", (specimen_id,)
        ).fetchone()[0])
        consumed_ledger = round(-sum(
            float(row[0]) for row in self.connection.execute(
                "SELECT quantity FROM custody_events WHERE specimen_id=? AND movement_type IN ('取样','领用','报废')",
                (specimen_id,),
            ).fetchall()
        ), 6)
        min_container = self.connection.execute(
            "SELECT COALESCE(MIN(quantity),0) FROM specimen_placements WHERE specimen_id=?", (specimen_id,)
        ).fetchone()[0]
        available_matches_ledger = abs(float(specimen["available_quantity"]) - expected_available) < 1e-6
        placements_match_available = abs(placed_weight - float(specimen["available_quantity"])) < 1e-6
        consumptions_match_ledger = abs(consumed_detail - consumed_ledger) < 1e-6
        no_negative_container = float(min_container) >= -1e-9
        return {
            "specimen_id": specimen_id,
            "recorded_available_grams": specimen["available_quantity"],
            "expected_available_grams": expected_available,
            "active_placement_grams": placed_weight,
            "consumed_grams": round(consumed_detail, 6),
            "consumed_ledger_grams": consumed_ledger,
            "available_matches_ledger": available_matches_ledger,
            "placements_match_available": placements_match_available,
            "consumptions_match_ledger": consumptions_match_ledger,
            "placements_within_available": placements_match_available and no_negative_container,
            "no_negative_container": no_negative_container,
            "conserved": all((
                available_matches_ledger, placements_match_available,
                consumptions_match_ledger, no_negative_container,
            )),
        }

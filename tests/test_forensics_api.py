from __future__ import annotations


def test_http_intake_and_custody_flow(client, admin):
    headers = admin["headers"]
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "HTTP-ORG-1", "agency_name": "区公安分局", "jurisdiction_code": "CN", "contact_address": "司法路 12 号",
        "restrictions": {},
    })
    assert source.status_code == 201, source.text
    forensic_case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "HTTP-CASE-1", "case_name": "交通事故痕迹鉴定", "discipline": "痕迹物证",
        "entrusted_matter": "车辆碰撞痕迹比对", "agency_id": source.json()["id"], "case_source": "委派",
        "accepted_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert forensic_case.status_code == 201, forensic_case.text
    accepted = client.post(f"/api/forensics/cases/{forensic_case.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "HTTP-L1", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": 3000, "reference_value": 4, "humidity_percent": 35,
    })
    assert location.status_code == 201, location.text
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "HTTP-SP-1", "case_id": accepted.json()["id"], "received_year": 2026,
        "initial_quantity": 8, "integrity_percent": 100, "packaging": "独立封装", "created_by": "登记员",
    })
    assert specimen.status_code == 201, specimen.text
    placed = client.post("/api/forensics/placements", headers=headers, json={
        "specimen_id": specimen.json()["id"], "location_id": location.json()["id"], "quantity": 8,
        "container_code": "HTTP-BOX-1", "idempotency_key": "http-place-0001", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    detail = client.get(f"/api/forensics/specimens/{specimen.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "stored"
    assert detail.json()["placements"][0]["container_code"] == "HTTP-BOX-1"


def test_api_rejects_unauthenticated_business_request(client):
    response = client.get("/api/forensics/dashboard")
    assert response.status_code == 401


def test_api_validation_error_has_structured_body(client, admin):
    response = client.post("/api/forensics/locations", headers=admin["headers"], json={
        "location_code": "BAD", "facility": "库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": -1, "reference_value": 4, "humidity_percent": 35,
    })
    assert response.status_code == 422
    assert response.json()["detail"]


def test_withdrawal_conservation_visible_everywhere(client, admin):
    headers = admin["headers"]
    source = client.post("/api/forensics/agencies", headers=headers, json={
        "agency_code": "HTTP-ORG-9", "agency_name": "区公安分局", "jurisdiction_code": "CN",
        "contact_address": "司法路 12 号", "restrictions": {},
    })
    case = client.post("/api/forensics/cases", headers=headers, json={
        "case_no": "HTTP-CASE-9", "case_name": "法医物证鉴定", "discipline": "法医物证",
        "entrusted_matter": "DNA 检验", "agency_id": source.json()["id"], "case_source": "委托",
        "accepted_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    }).json()
    client.post(f"/api/forensics/cases/{case['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    location = client.post("/api/forensics/locations", headers=headers, json={
        "location_code": "HTTP-L9", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_units": 3000, "reference_value": 4, "humidity_percent": 35,
    }).json()
    specimen = client.post("/api/forensics/specimens", headers=headers, json={
        "specimen_no": "HTTP-SP-9", "case_id": case["id"], "received_year": 2026,
        "initial_quantity": 20, "integrity_percent": 100, "packaging": "独立封装", "created_by": "登记员",
    }).json()
    for code, qty in [("HTTP-BOX-9A", 12), ("HTTP-BOX-9B", 8)]:
        response = client.post("/api/forensics/placements", headers=headers, json={
            "specimen_id": specimen["id"], "location_id": location["id"], "quantity": qty,
            "container_code": code, "idempotency_key": f"http-place-9-{code}", "actor": "保管员",
        })
        assert response.status_code == 201, response.text

    body = {
        "specimen_id": specimen["id"], "quantity": 5, "movement_type": "取样",
        "idempotency_key": "http-withdraw-9-0001", "actor": "技术员", "reason": "DNA 初检取样",
    }
    first = client.post("/api/forensics/withdrawals", headers=headers, json=body)
    assert first.status_code == 201, first.text
    replay = client.post("/api/forensics/withdrawals", headers=headers, json=body)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True

    # 检材总账
    detail = client.get(f"/api/forensics/specimens/{specimen['id']}", headers=headers).json()
    assert detail["available_quantity"] == 15
    # 容器余量与追溯：5 份来自 HTTP-BOX-9A（稳定默认次序）
    by_container = {p["container_code"]: p["remaining_quantity"] for p in detail["placements"] if p["removed_at"] is None}
    assert by_container == {"HTTP-BOX-9A": 7, "HTTP-BOX-9B": 8}
    movement = [m for m in detail["movements"] if m["movement_type"] == "取样"][0]
    assert movement["items"][0]["container_code"] == "HTTP-BOX-9A"
    assert movement["items"][0]["quantity"] == 5

    # 库位占用与总账一致
    location_view = client.get(f"/api/forensics/locations/{location['id']}", headers=headers).json()
    assert location_view["used_grams"] == 15
    assert location_view["available_grams"] == 2985

    # 守恒结果
    report = client.get(f"/api/forensics/specimens/{specimen['id']}/reconcile", headers=headers).json()
    assert report["conservation_ok"] is True

    # 同键不同数量 => 409 冲突
    conflict = client.post("/api/forensics/withdrawals", headers=headers, json={**body, "quantity": 6})
    assert conflict.status_code == 409

"""pIES 最小案例脚本: 最小闭环(登录→项目→设备→数据→配置→任务→结果→导出)。

在 docker 内运行: docker compose exec backend python /app/tests/minimal_case.py
用于动态工作流驱动的 bug 发现与修复循环。
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = "http://web"  # 从 backend 容器访问 nginx

# 管理员(当前数据库状态: 已改密)
ADMIN_PW = "Iesplan-Admin#Verify2026"
ENG_PW = "Iesplan-Eng#2026e2e"

PASS, FAIL = 0, 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  ✗ {name} -- {detail}")


def main() -> int:
    print("=" * 60)
    print("pIES 最小案例")
    print("=" * 60)
    c = httpx.Client(base_url=BASE, timeout=120.0)

    # ---------- 1. 管理员登录(独立 client, 避免共享 cookie 触发接管) ----------
    ac = httpx.Client(base_url=BASE, timeout=120.0)
    r = ac.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    if r.status_code == 429:
        print("  admin 被限速, 等待 60s...")
        time.sleep(60)
        r = ac.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    check("管理员登录", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    admin = r.json()
    if admin.get("needs_takeover_confirm"):
        # 有残留会话: 确认接管
        r = ac.post("/api/auth/confirm-takeover", json={"token": admin.get("token")})
        check("确认接管", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
        admin = r.json()
    at = admin.get("token")
    ah = {"Authorization": f"Bearer {at}"}

    # ---------- 2. 创建工程师并登录(独立 client) ----------
    ts = time.strftime("%m%d%H%M%S")
    eng_name = f"min_{ts}"
    r = c.put("/api/auth/settings", json={"registration_enabled": True}, headers=ah)
    if r.status_code != 200:
        check("开启注册", False, f"{r.status_code} {r.text[:200]}")
    r = c.post("/api/auth/register", json={"username": eng_name, "password": ENG_PW,
                                           "display_name": f"最小案例 {ts}"})
    check("创建工程师(注册)", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    ec = httpx.Client(base_url=BASE, timeout=120.0)
    r = ec.post("/api/auth/login", json={"username": eng_name, "password": ENG_PW})
    check("工程师登录", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    e = r.json()
    eh = {"Authorization": f"Bearer {e.get('token')}"}

    # ---------- 3. 创建项目 ----------
    r = ec.post("/api/projects", json={"name": f"最小案例 {ts}", "currency": "CNY",
                                       "baseline_resolution": "1h", "baseline_leap_year": False, "baseline_scenario_mode": "single"}, headers=eh)
    check("创建项目", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
    pid = r.json().get("project", {}).get("id") or r.json().get("id")
    check("项目 id", bool(pid), f"{r.text[:200]}")

    # ---------- 4. 建模: 最小设备集(电网+电负荷+热负荷+热泵) ----------
    devs = {}
    for t, name, is_new, params in [
        ("ies.device.grid_connection", "电网", False, {"max_import_power_kw": 500}),
        ("ies.device.electric_load", "电负荷", False, {"peak_power_kw": 300, "load_profile": "e_load"}),
        ("ies.device.heat_load", "热负荷", False, {"peak_heat_kw": 200, "heat_profile": "h_load"}),
        ("ies.device.heat_pump", "热泵", True, {"rated_heat_kw": 250, "mode": "heating"}),
    ]:
        r = ec.post(f"/api/projects/{pid}/model/devices",
                   json={"device_type": t, "name": name, "params": params,
                         "is_existing": not is_new, "model_precision": "medium"}, headers=eh)
        if r.status_code in (200, 201):
            devs[name] = r.json().get("device", {}).get("id")
        else:
            check(f"创建设备 {name}", False, f"{r.status_code} {r.text[:300]}")
    check("4 类设备创建", len(devs) == 4, f"created {len(devs)}/4")

    # 连接
    r = ec.get(f"/api/projects/{pid}/model", headers=eh)
    graph = r.json().get("graph", r.json())
    ports = graph.get("ports", [])
    dev_ports = {}
    for p in ports:
        dev_ports.setdefault(p.get("device_id"), []).append(p)
    conns = 0
    for src, dst in [("电网", "电负荷"), ("电网", "热泵"), ("热泵", "热负荷")]:
        sp = [p for p in dev_ports.get(devs.get(src), []) if p.get("direction") == "out"]
        dp = [p for p in dev_ports.get(devs.get(dst), []) if p.get("direction") == "in"]
        if sp and dp:
            r = ec.post(f"/api/projects/{pid}/model/connections",
                       json={"from_port_id": sp[0]["id"], "to_port_id": dp[0]["id"], "attrs": {}},
                       headers=eh)
            if r.status_code in (200, 201):
                conns += 1
    check("3 条连接", conns == 3, f"{conns}/3")

    # ---------- 5. 数据: 内置样例(1h) ----------
    r = ec.post(f"/api/projects/{pid}/datasets",
               json={"name": "min sample", "source_category": "builtin_sample",
                     "license": "internal", "provenance": {"source": "min_case"}}, headers=eh)
    ds = r.json().get("dataset", {}).get("id") or r.json().get("dataset_version", {}).get("dataset_id") or r.json().get("id")
    check("创建数据集", r.status_code in (200, 201) and ds, f"{r.status_code} {r.text[:200]}")
    r = ec.post(f"/api/projects/{pid}/datasets/{ds}/sample", json={"resolution": "1h"}, headers=eh)
    check("生成样例数据", r.status_code in (200, 201), f"{r.status_code} {r.text[:400]}")
    dv_id = None
    if r.status_code in (200, 201):
        dv = r.json().get("dataset_version", r.json())
        dv_id = dv.get("id") or dv.get("dataset_version_id")
    # 绑定数据集版本到草稿(dataset.bind 语义命令)
    if dv_id:
        r = ec.get(f"/api/projects/{pid}", headers=eh)
        pv = r.json().get("project", r.json())
        rev = (r.json().get("draft") or {}).get("revision") or 1
        cmd = {"id": f"bind-{ds}-{ts}", "project_id": pid, "expected_revision": rev,
               "session": "min-case", "unit": "dataset", "type": "dataset.bind",
               "payload": {"dataset_version_id": dv_id, "dataset_id": ds, "role": "annual"}}
        r = ec.put(f"/api/projects/{pid}/draft",
                   json={"expected_revision": rev, "commands": [cmd]}, headers=eh)
        check("绑定数据集版本", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")

    # ---------- 6. 配置 + 基准确认 ----------
    r = ec.get(f"/api/projects/{pid}/config/default", headers=eh)
    check("默认配置", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
    cfg = r.json().get("config", r.json())
    # 先取当前配置(含草稿修订号)再保存
    # 修订号: 从项目视图的 draft.revision 取(绑定命令后修订已推进)
    rv = ec.get(f"/api/projects/{pid}", headers=eh)
    rev = 1
    if rv.status_code in (200, 201):
        rev = (rv.json().get("draft") or {}).get("revision") or 1
    r = ec.put(f"/api/projects/{pid}/config", json={"config": cfg, "expected_revision": rev}, headers=eh)
    check("保存配置", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")
    r = ec.post(f"/api/projects/{pid}/validation/baseline-confirm",
               json={"assumptions": {"desc": "基准: 仅存量设备"}}, headers=eh)
    check("基准确认", r.status_code in (200, 409), f"{r.status_code} {r.text[:200]}")

    # ---------- 7. 校验 ----------
    r = ec.post(f"/api/projects/{pid}/validation/run", headers=eh)
    vrep = r.json()
    blocked = vrep.get("report", vrep).get("blocks_submit", False)
    diags = vrep.get("report", vrep).get("diagnostics", [])
    print(f"  校验诊断 {len(diags)} 条, blocks_submit={blocked}")
    for d in diags[:8]:
        print(f"    - [{d.get('code')}] {d.get('severity')} {d.get('message_key')}")
    check("校验通过(无阻断)", not blocked, f"{[d.get('code') for d in diags][:8]}")

    # ---------- 8. 提交方案评价任务并轮询 ----------
    r = ec.post(f"/api/projects/{pid}/tasks",
               json={"task_type": "calc", "idempotency_key": f"min-{ts}"}, headers=eh)
    check("提交任务", r.status_code in (200, 201), f"{r.status_code} {r.text[:400]}")
    task = r.json().get("task", r.json())
    tid = task.get("id")
    state = None
    for _ in range(60):
        time.sleep(5)
        r = ec.get(f"/api/projects/{pid}/tasks/{tid}", headers=eh)
        if r.status_code != 200:
            check("查询任务", False, f"{r.status_code} {r.text[:200]}")
            break
        task = r.json().get("task", r.json())
        state = task.get("status")
        print(f"  · [{_*5}s] {state}")
        if state in ("completed", "failed", "cancelled", "timed_out"):
            break
    check("eval 任务完成", state == "completed", f"最终: {state}")

    # ---------- 9. 结果视图 ----------
    if state == "completed":
        r = ec.get(f"/api/projects/{pid}/tasks/{tid}/result", headers=eh)
        check("结果视图", r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}")

    # ---------- 10. Excel 导出 ----------
    ep_id = None
    as_id = None
    r = ec.get(f"/api/projects/{pid}/tasks/{tid}/result", headers=eh)
    if r.status_code in (200, 201):
        rv = r.json().get("result", r.json())
        ev = rv.get("evidence") or {}
        ep_id = rv.get("evidence_package_id") or ev.get("id") or ev.get("evidence_package_id")
        asr = ec.get(f"/api/projects/{pid}/tasks/{tid}/result/assessments", headers=eh)
        if asr.status_code in (200, 201):
            assessments = asr.json().get("items", asr.json().get("assessments", []))
            if assessments:
                as_id = assessments[-1].get("id")
    r = ec.post(f"/api/projects/{pid}/exports/excel",
                json={"lang": "zh", "evidence_package_id": ep_id, "assessment_id": as_id}, headers=eh)
    if r.status_code in (200, 201):
        xd = r.json()
        tok = xd.get("token")
        r2 = ec.get(f"/api/projects/{pid}/exports/excel/download?token={tok}", headers=eh) if tok else None
        ok = r2 is not None and r2.status_code in (200, 201) and len(r2.content) > 500
        check("Excel 导出", bool(ok),
              f"{r2.status_code if r2 else 'no token'} {len(r2.content) if r2 else 0}B {r2.content[:50] if r2 and r2.status_code != 200 else ''}")
    else:
        check("Excel 导出", False, f"{r.status_code} {r.text[:300]}")

    # ---------- 汇总 ----------
    print("=" * 60)
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

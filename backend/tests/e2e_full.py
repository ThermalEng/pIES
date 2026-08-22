"""pIES 全栈端到端验证脚本(真实 HTTP, 走 nginx -> backend)。

运行方式(在 docker 容器内执行, 不污染主机):
    docker run --rm --network host -v <repo>/backend/tests:/tests ies_plan-backend python /tests/e2e_full.py

覆盖核心闭环:
    1. 管理员登录 -> 首次改密 -> 重新登录
    2. 开启注册 -> 创建工程师账号 x2(管理员动作)
    3. 工程师登录 -> 创建项目(CNY, +08:00)
    4. 建模: 9 类设备(电网/光伏/电池/热泵/锅炉/制冷机/电负荷/热负荷/冷负荷)+ 连接
    5. 数据: 创建数据集 + 内置样例(1h)
    6. 配置: 保存默认配置 + 财务基准确认
    7. 校验: validation run 无阻断错误
    8. 方案评价(calc)任务 -> completed -> 结果视图(四维评估)
    9. 规划(optimization)任务 -> completed -> 候选列表与 IRR
    10. 选择结果 -> 差异预览 -> 应用结果(新版本)
    11. Excel 导出(zh) -> 下载 xlsx
    12. 项目包导出(所有者) -> 下载; 查看者包导出 403; 查看者 Excel 导出成功
    13. 项目包导入(另一工程师) -> 新项目身份/所有者正确
    14. 归档 -> 禁止编辑 -> 撤销归档
    15. 审计查询(管理员) -> 事件存在
    16. 存储视图 + 健康端点
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime

import httpx

# 从 backend 容器内访问 nginx(web 容器): http://web ; 从主机访问: http://localhost:8080
BASE = "http://web"
ADMIN_INIT_PASSWORD = "iesplan-admin-initial"
ADMIN_NEW_PASSWORD = "Iesplan-Admin#2026e2e"
ENG_PASSWORD = "Iesplan-Eng#2026e2e"

results: list[tuple[str, bool, str]] = []
step_no = 0


def step(name: str) -> None:
    global step_no
    step_no += 1
    results.append((f"{step_no:02d}. {name}", False, "pending"))


def ok(detail: str = "") -> None:
    name, _, _ = results[-1]
    results[-1] = (name, True, detail)
    print(f"  PASS | {name} | {detail}", flush=True)


def fail(detail: str) -> None:
    name, _, _ = results[-1]
    results[-1] = (name, False, detail)
    print(f"  FAIL | {name} | {detail}", flush=True)


class Client:
    """带窗口凭证(Bearer)与 X-User-Id 的业务客户端。"""

    def __init__(self) -> None:
        self.http = httpx.Client(base_url=BASE, timeout=60.0)
        self.token: str | None = None
        self.user_id: int | None = None

    def headers(self, user_id: int | None = None) -> dict:
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if user_id is not None:
            h["X-User-Id"] = str(user_id)
        return h

    def post(self, path: str, json=None, user_id: int | None = None, **kw):
        return self.http.post(path, json=json, headers=self.headers(user_id), **kw)

    def put(self, path: str, json=None, user_id: int | None = None, **kw):
        return self.http.put(path, json=json, headers=self.headers(user_id), **kw)

    def get(self, path: str, user_id: int | None = None, **kw):
        return self.http.get(path, headers=self.headers(user_id), **kw)


def login(c: Client, username: str, password: str) -> dict:
    r = c.post("/api/auth/login", json={"username": username, "password": password, "device": "e2e"})
    r.raise_for_status()
    data = r.json()
    c.token = data["token"]
    c.user_id = data["user"]["id"]
    return data


def poll_task(c: Client, project_id: int, task_id: int, timeout_s: int = 900) -> dict:
    """轮询任务直到终态, 返回任务摘要 dict。"""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        r = c.get(f"/api/projects/{project_id}/tasks/{task_id}", user_id=c.user_id)
        r.raise_for_status()
        task = r.json()["task"]
        last = task
        st = task["status"]
        if st in ("completed", "failed", "cancelled"):
            return task
        time.sleep(3)
    raise TimeoutError(f"任务 {task_id} 在 {timeout_s}s 内未完成, 最后状态: {last}")


def main() -> int:
    c = Client()
    ts = datetime.now().strftime("%m%d%H%M%S")
    suffix = f"{ts}{uuid.uuid4().hex[:4]}"

    # ------------------------------------------------------------------
    step("管理员登录(admin/初始密码)")
    admin_initial_password = ADMIN_INIT_PASSWORD
    try:
        r = c.post("/api/auth/login", json={"username": "admin", "password": admin_initial_password})
        if r.status_code == 401:
            # 容忍上次运行已改密: 用新密码重试(保证脚本可重复执行)
            r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_NEW_PASSWORD})
            assert r.status_code == 200, f"登录失败: {r.status_code} {r.text[:200]}"
            admin_initial_password = ADMIN_NEW_PASSWORD
            data = r.json()
            c.token = data["token"]
            ok(f"user_id={data['user']['id']}(密码已在先前运行修改, 跳过本次改密)")
            results[-1] = (results[-1][0], True, f"user_id={data['user']['id']}(先前已改密)")
        else:
            assert r.status_code == 200, f"登录失败: {r.status_code} {r.text[:200]}"
            data = r.json()
            assert data["user"]["force_password_change"] is True, "初始管理员应强制改密"
            assert data["user"]["role"] == "admin"
            c.token = data["token"]
            ok(f"user_id={data['user']['id']}")
    except Exception as exc:  # noqa: BLE001
        fail(f"管理员登录失败: {exc}")
        return 1

    step("首次改密(强制改密后旧会话失效, 须重新登录)")
    try:
        if admin_initial_password == ADMIN_NEW_PASSWORD:
            # 先前运行已改密: 验证新密码会话可用即视为通过
            r = c.post("/api/auth/refresh")
            assert r.status_code == 200, f"会话续期失败: {r.status_code} {r.text[:200]}"
            ok("先前已改密, 新密码会话有效(refresh 通过)")
        else:
            r = c.post("/api/auth/change-password", json={
                "old_password": ADMIN_INIT_PASSWORD, "new_password": ADMIN_NEW_PASSWORD})
            assert r.status_code == 200, f"改密失败: {r.status_code} {r.text[:200]}"
            # 改密后凭证版本轮换, 旧 token 立即失效
            r = c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_NEW_PASSWORD})
            assert r.status_code == 200, f"新密码登录失败: {r.status_code} {r.text[:200]}"
            c.token = r.json()["token"]
            ok("改密成功且新密码可登录")
    except Exception as exc:  # noqa: BLE001
        fail(f"改密流程失败: {exc}")
        return 1

    step("管理员开启注册并创建工程师账号 x2")
    eng1 = eng2 = None
    try:
        # 注: 注册开关为进程内存态, backend 双 worker 下需重试以命中开启标志的进程
        def register_user(username: str) -> dict:
            for _ in range(8):
                r = c.put("/api/auth/settings", json={"registration_enabled": True})
                assert r.status_code == 200
                r = c.post("/api/auth/register", json={
                    "username": username, "password": ENG_PASSWORD,
                    "display_name": f"工程师 {username}"})
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 403:
                    time.sleep(0.5)
                    continue
                raise AssertionError(f"注册 {username} 失败: {r.status_code} {r.text[:200]}")
            raise AssertionError(f"注册 {username} 重试耗尽(双 worker 注册开关不一致)")

        for name in (f"eng_a_{suffix}", f"eng_b_{suffix}"):
            u = register_user(name)
            assert u["role"] == "engineer"
            if eng1 is None:
                eng1 = u
            else:
                eng2 = u
        ok(f"eng1={eng1['username']}(id={eng1['id']}) eng2={eng2['username']}(id={eng2['id']})")
    except Exception as exc:  # noqa: BLE001
        fail(f"工程师账号创建失败: {exc}")
        return 1

    step("工程师登录并创建项目(CNY, +08:00)")
    e1 = Client()
    project_id = None
    try:
        login(e1, eng1["username"], ENG_PASSWORD)
        assert e1.user_id == eng1["id"]
        r = e1.post("/api/projects", user_id=e1.user_id, json={
            "name": f"E2E 综合能源项目 {suffix}",
            "currency": "CNY",
            "utc_offset_minutes": 480,
            "description": "全栈端到端验证项目",
        })
        assert r.status_code == 201, f"创建项目失败: {r.status_code} {r.text[:300]}"
        project = r.json()["project"]
        project_id = project["id"]
        assert project["currency"] == "CNY" and project["fixed_utc_offset_minutes"] == 480
        assert r.json()["my_role"] == "owner"
        ok(f"project_id={project_id}")
    except Exception as exc:  # noqa: BLE001
        fail(f"工程师登录/创建项目失败: {exc}")
        return 1

    step("建模: 创建 9 类设备并连接(电网/光伏/电池/热泵/锅炉/制冷机/电负荷/热负荷/冷负荷)")
    device_ids: dict[str, int] = {}
    try:
        # 存量设备: 电网/锅炉/制冷机/三类负荷(基线可行, 覆盖峰值负荷);
        # 新增设备: 光伏/电池/热泵(规划引擎容量枚举对象, 见 engines/planning.py)
        specs = [
            ("grid", "ies.device.grid_connection", {
                "max_import_power_kw": 5000, "max_export_power_kw": 1000,
                "voltage_level_kv": 10, "import_tariff": {"peak": 1.1, "flat": 0.7, "valley": 0.3},
                "export_tariff": 0.35}, True),
            ("pv", "ies.device.pv", {
                "rated_capacity_kwp": 500, "max_capacity_kwp": 2000, "efficiency": 0.20}, False),
            ("battery", "ies.device.battery", {
                "capacity_kwh": 1000, "max_capacity_kwh": 4000, "rated_power_kw": 500}, False),
            ("heat_pump", "ies.device.heat_pump", {
                "rated_heat_kw": 800, "max_heat_kw": 2000, "cop": 3.2, "mode": "both"}, False),
            ("gas_boiler", "ies.device.gas_boiler", {
                "rated_heat_kw": 1200, "max_heat_kw": 1600, "thermal_efficiency": 0.90}, True),
            ("electric_chiller", "ies.device.electric_chiller", {
                "rated_cooling_kw": 1200, "max_cooling_kw": 1600, "cop": 4.0}, True),
            ("electric_load", "ies.device.electric_load",
             {"peak_power_kw": 1200, "load_profile": "ref:e_load"}, True),
            ("heat_load", "ies.device.heat_load",
             {"peak_heat_kw": 800, "heat_profile": "ref:h_load"}, True),
            ("cooling_load", "ies.device.cooling_load",
             {"peak_cooling_kw": 700, "cooling_profile": "ref:c_load"}, True),
        ]
        positions = [(i * 120, 100 + (i % 3) * 140) for i in range(len(specs))]
        for i, (key, dtype, params, is_existing) in enumerate(specs):
            r = e1.post(f"/api/projects/{project_id}/model/devices", user_id=e1.user_id, json={
                "device_type": dtype, "name": f"{key}_{suffix}", "params": params,
                "is_existing": is_existing,
                "position": {"x": positions[i][0], "y": positions[i][1]}})
            assert r.status_code == 201, f"创建设备 {key} 失败: {r.status_code} {r.text[:300]}"
            device_ids[key] = r.json()["device"]["id"]
        # 读取端口拓扑, 建立连接(源 out/bidirectional -> 汇 in/bidirectional, 同载体)
        r = e1.get(f"/api/projects/{project_id}/model", user_id=e1.user_id)
        assert r.status_code == 200
        graph = r.json()
        ports = graph["ports"]
        by_key: dict[str, list[dict]] = {}
        for key, dev_id in device_ids.items():
            by_key[key] = [p for p in ports if p["device_id"] == dev_id]
        def port(key: str, ptype: str) -> dict:
            for p in by_key[key]:
                if p["port_type"] == ptype:
                    return p
            raise AssertionError(f"设备 {key} 无 {ptype} 端口: {by_key[key]}")

        pairs = [
            ("grid", "electric", "out", "electric_load", "electric", "in"),
            ("grid", "electric", "out", "battery", "electric", "in"),
            ("grid", "electric", "out", "heat_pump", "electric", "in"),
            ("grid", "electric", "out", "electric_chiller", "electric", "in"),
            ("pv", "electric", "out", "electric_load", "electric", "in"),
            ("battery", "electric", "out", "electric_load", "electric", "in"),
            ("heat_pump", "thermal", "out", "heat_load", "thermal", "in"),
            ("gas_boiler", "thermal", "out", "heat_load", "thermal", "in"),
            ("heat_pump", "cooling", "out", "cooling_load", "cooling", "in"),
            ("electric_chiller", "cooling", "out", "cooling_load", "cooling", "in"),
        ]
        n_conn = 0
        for fk, fp, _, tk, tp, _ in pairs:
            f = port(fk, fp)
            t = port(tk, tp)
            r = e1.post(f"/api/projects/{project_id}/model/connections", user_id=e1.user_id, json={
                "from_port_id": f["id"], "to_port_id": t["id"]})
            assert r.status_code == 201, (
                f"连接 {fk}.{fp}->{tk}.{tp} 失败: {r.status_code} {r.text[:300]}")
            n_conn += 1
        assert n_conn == 10
        ok(f"设备 {len(device_ids)} 台, 连接 {n_conn} 条")
    except Exception as exc:  # noqa: BLE001
        fail(f"建模失败: {exc}")
        return 1

    step("数据: 创建数据集并生成内置样例(1h)")
    dataset_version_id = None
    try:
        r = e1.post(f"/api/projects/{project_id}/datasets", user_id=e1.user_id, json={
            "name": f"样例数据集 {suffix}", "description": "1h 内置样例"})
        assert r.status_code == 201, f"创建数据集失败: {r.status_code} {r.text[:300]}"
        dataset_id = r.json()["dataset"]["id"]
        r = e1.post(
            f"/api/projects/{project_id}/datasets/{dataset_id}/sample",
            user_id=e1.user_id,
            params={"resolution": "1h", "region": "shanghai"})
        assert r.status_code == 201, f"生成样例失败: {r.status_code} {r.text[:300]}"
        body = r.json()
        dataset_version_id = body["dataset_version"]["id"]
        report = body.get("quality_report") or {}
        assert not report.get("has_blocking_errors"), f"样例质量报告含阻断错误: {report}"
        assert body["dataset_version"]["resolution"] == "1h"
        # 绑定数据集版本到项目草稿(revision 1 -> 2)
        r = e1.put(f"/api/projects/{project_id}/draft", user_id=e1.user_id, json={
            "expected_revision": 1,
            "commands": [{
                "id": f"e2e-bind-{suffix}", "unit": "dataset",
                "type": "dataset.bind", "payload": {"dataset_version_id": dataset_version_id},
            }],
        })
        assert r.status_code == 200, f"数据集绑定失败: {r.status_code} {r.text[:300]}"
        assert r.json()["revision"] == 2
        ok(f"dataset_id={dataset_id} version_id={dataset_version_id} 已绑定(rev=2)")
    except Exception as exc:  # noqa: BLE001
        fail(f"数据准备失败: {exc}")
        return 1

    step("配置: 保存默认配置 + 财务基准确认")
    try:
        r = e1.get(f"/api/projects/{project_id}/config/default", user_id=e1.user_id)
        assert r.status_code == 200, f"默认配置读取失败: {r.status_code} {r.text[:300]}"
        cfg = r.json()["config"]
        r = e1.put(f"/api/projects/{project_id}/config", user_id=e1.user_id, json={
            "config": cfg, "expected_revision": 2})
        assert r.status_code == 200, f"配置保存失败: {r.status_code} {r.text[:300]}"
        assert r.json()["diagnostics"] == []
        # 财务基准确认(假设与默认配置经济参数一致)
        econ = (cfg.get("parameters") or {}).get("economic") or {}
        assumptions = {
            "discount_rate": econ.get("discount_rate"),
            "tax_rate": econ.get("tax_rate"),
            "project_years": econ.get("project_years"),
            "depreciation_years": econ.get("depreciation_years"),
            "currency": "CNY",
            "irr_floor": cfg.get("irr_floor"),
        }
        r = e1.post(f"/api/projects/{project_id}/validation/baseline-confirm",
                    user_id=e1.user_id, json={"assumptions": assumptions})
        assert r.status_code == 200, f"基准确认失败: {r.status_code} {r.text[:300]}"
        assert r.json()["confirmed"] is True and r.json().get("assumptions_hash")
        ok("默认配置已保存, 财务基准确认已记录")
    except Exception as exc:  # noqa: BLE001
        fail(f"配置/基准确认失败: {exc}")
        return 1

    step("校验: validation run 通过(无阻断错误)")
    try:
        r = e1.post(f"/api/projects/{project_id}/validation/run", user_id=e1.user_id)
        assert r.status_code == 200, f"校验失败: {r.status_code} {r.text[:300]}"
        report = r.json()["report"]
        blockers = [d for d in report["diagnostics"] if d.get("blocking")]
        if blockers:
            raise AssertionError(f"校验存在阻断错误: {[d['code'] for d in blockers]}")
        assert report["status"] in ("ok", "warnings")
        ok(f"校验状态={report['status']}, 诊断 {len(report['diagnostics'])} 条(无阻断)")
    except Exception as exc:  # noqa: BLE001
        fail(f"校验失败: {exc}")
        return 1

    step("方案评价(calc): 提交任务 -> completed -> 结果视图四维评估")
    eval_task_id = None
    try:
        r = e1.post(f"/api/projects/{project_id}/tasks", user_id=e1.user_id, json={
            "task_type": "calc",
            "config": {"resolution": "1h", "solver_options": {"timeout": 300, "mip_rel_gap": 0.001}},
            "idempotency_key": f"e2e-calc-{suffix}",
        })
        assert r.status_code == 201, f"提交 calc 任务失败: {r.status_code} {r.text[:300]}"
        eval_task_id = r.json()["task"]["id"]
        task = poll_task(e1, project_id, eval_task_id, timeout_s=1500)
        assert task["status"] == "completed", f"calc 任务未完成: {task}"
        r = e1.get(f"/api/projects/{project_id}/tasks/{eval_task_id}/result", user_id=e1.user_id)
        assert r.status_code == 200
        view = r.json()["result"]
        assessment = view.get("assessment")
        assert assessment is not None, "结果视图缺少四维评估"
        dims = assessment["dimensions"]
        for k in ("physical", "optimality", "financial", "reliability"):
            assert dims.get(k) in ("pass", "fail", "unknown"), f"维度 {k} 非法: {dims.get(k)}"
        assert view.get("metrics_summary") is not None, "结果视图缺少指标摘要"
        assert view["metrics_summary"].get("annual_buy_kwh") is not None, "指标缺少购电量(方案应可解)"
        ok(f"task_id={eval_task_id} 四维评估={dims} 购电={view['metrics_summary']['annual_buy_kwh']:.0f}kWh")
    except Exception as exc:  # noqa: BLE001
        fail(f"方案评价失败: {exc}")
        return 1

    step("规划(optimization): 提交任务 -> completed -> 候选列表与 IRR")
    plan_task_id = None
    try:
        r = e1.post(f"/api/projects/{project_id}/tasks", user_id=e1.user_id, json={
            "task_type": "optimization",
            "config": {
                "resolution": "1h",
                "planning_options": {
                    "max_combinations": 8, "timeout_per_eval": 90,
                    "irr_floor": 0.005, "seed": 42,
                },
            },
            "idempotency_key": f"e2e-plan-{suffix}",
        })
        assert r.status_code == 201, f"提交规划任务失败: {r.status_code} {r.text[:300]}"
        plan_task_id = r.json()["task"]["id"]
        task = poll_task(e1, project_id, plan_task_id, timeout_s=1800)
        assert task["status"] == "completed", f"规划任务未完成: {task}"
        r = e1.get(f"/api/projects/{project_id}/tasks/{plan_task_id}/result", user_id=e1.user_id)
        assert r.status_code == 200
        view = r.json()["result"]
        candidates = view.get("candidates") or []
        assert candidates, "规划结果无候选列表(需要 IRR >= 下限的候选)"
        irrs = [cand["irr"] for cand in candidates]
        assert all(i is not None for i in irrs), f"候选缺少 IRR: {candidates[:3]}"
        assert irrs == sorted(irrs, reverse=True), "候选应按 IRR 降序"
        ok(f"task_id={plan_task_id} 候选 {len(candidates)} 个, IRR 范围 [{min(irrs):.4f}, {max(irrs):.4f}]")
    except Exception as exc:  # noqa: BLE001
        fail(f"规划失败: {exc}")
        return 1

    step("选择结果 -> 差异预览 -> 应用结果(创建新版本)")
    try:
        r = e1.post(
            f"/api/projects/{project_id}/tasks/{plan_task_id}/result/select",
            user_id=e1.user_id,
            json={"solution_id": 0, "selection_type": "adopt", "reason": "e2e 选择最优候选"})
        assert r.status_code == 201, f"选择结果失败: {r.status_code} {r.text[:300]}"
        diff = r.json().get("diff")
        assert diff is not None and diff.get("diff_patch"), "选择响应缺少差异补丁"
        r = e1.get(f"/api/projects/{project_id}/tasks/{plan_task_id}/result/diff", user_id=e1.user_id)
        assert r.status_code == 200
        diff = r.json()["diff"]
        patch = diff["diff_patch"]
        r = e1.post(f"/api/projects/{project_id}/apply-result", user_id=e1.user_id, json={
            "diff_patch": patch, "name": f"应用规划结果 {suffix}",
            "description": "E2E 应用选中候选", "source_result_id": str(plan_task_id)})
        assert r.status_code == 200, f"应用结果失败: {r.status_code} {r.text[:300]}"
        r = e1.get(f"/api/projects/{project_id}/versions", user_id=e1.user_id)
        assert r.status_code == 200
        versions = r.json()["versions"]
        assert versions and any(v["name"] == f"应用规划结果 {suffix}" for v in versions), \
            f"未找到新版本: {[v['name'] for v in versions]}"
        ok(f"已创建新版本: {versions[0]['name']}(版本号 {versions[0]['version_no']})")
    except Exception as exc:  # noqa: BLE001
        fail(f"选择/应用结果失败: {exc}")
        return 1

    step("Excel 导出(zh) -> 下载 xlsx")
    try:
        r = e1.get(f"/api/projects/{project_id}/tasks/{eval_task_id}/result", user_id=e1.user_id)
        view = r.json()["result"]
        evidence_id = view["evidence"]["id"]
        assessment_id = view["assessment"]["id"]
        r = e1.post(f"/api/projects/{project_id}/exports/excel", user_id=e1.user_id, json={
            "evidence_package_id": evidence_id, "assessment_id": assessment_id, "lang": "zh"})
        assert r.status_code == 200, f"Excel 导出失败: {r.status_code} {r.text[:300]}"
        token = r.json()["token"]
        r = e1.get(f"/api/projects/{project_id}/exports/excel/download",
                   user_id=e1.user_id, params={"token": token})
        assert r.status_code == 200, f"Excel 下载失败: {r.status_code} {r.text[:200]}"
        content_type = r.headers.get("content-type", "")
        body = r.content
        assert len(body) > 1000, f"Excel 内容过小: {len(body)}"
        assert body[:2] == b"PK", "xlsx 应为 zip 魔数 PK"
        ok(f"xlsx {len(body)} 字节, content-type={content_type}")
    except Exception as exc:  # noqa: BLE001
        fail(f"Excel 导出失败: {exc}")
        return 1

    step("项目包导出(所有者) -> 下载; 查看者包导出 403; 查看者 Excel 导出成功")
    package_token = None
    try:
        r = e1.post(f"/api/projects/{project_id}/exports/package", user_id=e1.user_id)
        assert r.status_code == 200, f"项目包导出失败: {r.status_code} {r.text[:300]}"
        package_token = r.json()["token"]
        r = e1.get(f"/api/projects/{project_id}/exports/package/download",
                   user_id=e1.user_id, params={"token": package_token})
        assert r.status_code == 200 and r.content[:2] == b"PK", "项目包下载失败"
        ok(f"项目包 zip {len(r.content)} 字节")
        # 添加查看者 eng2
        r = e1.put(f"/api/projects/{project_id}/viewers", user_id=e1.user_id,
                   json={"user_id": eng2["id"], "action": "add"})
        assert r.status_code == 200, f"添加查看者失败: {r.status_code} {r.text[:300]}"
        # 查看者包导出 -> 403
        e2 = Client()
        login(e2, eng2["username"], ENG_PASSWORD)
        r = e2.post(f"/api/projects/{project_id}/exports/package", user_id=e2.user_id)
        assert r.status_code == 403, f"查看者导出项目包应为 403, 实际 {r.status_code} {r.text[:200]}"
        # 查看者 Excel 导出 -> 成功
        r = e2.post(f"/api/projects/{project_id}/exports/excel", user_id=e2.user_id, json={
            "evidence_package_id": evidence_id, "assessment_id": assessment_id, "lang": "zh"})
        assert r.status_code == 200, f"查看者 Excel 导出失败: {r.status_code} {r.text[:200]}"
        ok("查看者包导出 403, 查看者 Excel 导出成功")
    except Exception as exc:  # noqa: BLE001
        fail(f"项目包导出/权限检查失败: {exc}")
        return 1

    step("项目包导入(另一工程师) -> 新项目身份/所有者正确")
    imported_project_id = None
    try:
        r = e1.get(f"/api/projects/{project_id}/exports/package/download",
                   user_id=e1.user_id, params={"token": package_token})
        assert r.status_code == 200
        zip_bytes = r.content
        e2 = Client()
        login(e2, eng2["username"], ENG_PASSWORD)
        r = e2.post("/api/projects/import", user_id=e2.user_id,
                    files={"file": ("pkg.zip", zip_bytes, "application/zip")})
        assert r.status_code in (200, 201), f"导入提案失败: {r.status_code} {r.text[:300]}"
        proposal = r.json()["proposal"]
        assert proposal["status"] == "proposed"
        r = e2.post(f"/api/projects/import/{proposal['id']}/confirm", user_id=e2.user_id)
        assert r.status_code == 201, f"确认导入失败: {r.status_code} {r.text[:300]}"
        imported = r.json()["project"]
        imported_project_id = imported["id"]
        assert imported_project_id != project_id, "导入项目必须是新身份"
        assert imported["owner_id"] == eng2["id"], f"导入者应为所有者, 实际 {imported['owner_id']}"
        assert imported["status"] == "active"
        ok(f"imported_project_id={imported_project_id}, owner=eng2, 名称={imported['name']}")
    except Exception as exc:  # noqa: BLE001
        fail(f"项目包导入失败: {exc}")
        return 1

    step("归档项目 -> 禁止编辑 -> 撤销归档")
    try:
        r = e1.post(f"/api/projects/{project_id}/archive", user_id=e1.user_id)
        assert r.status_code == 200 and r.json()["project"]["status"] == "archived"
        # 归档后编辑应被拒绝(草稿命令走项目状态门禁)
        r = e1.put(f"/api/projects/{project_id}/draft", user_id=e1.user_id, json={
            "expected_revision": 2, "commands": [{
                "id": f"e2e-edit-archived-{suffix}", "unit": "dataset",
                "type": "dataset.unbind", "payload": {"dataset_version_id": dataset_version_id}}]})
        assert r.status_code == 409, f"归档后编辑应 409, 实际 {r.status_code} {r.text[:200]}"
        # 提交任务也应被拒绝
        r = e1.post(f"/api/projects/{project_id}/tasks", user_id=e1.user_id,
                    json={"task_type": "calc", "idempotency_key": f"e2e-arch-{suffix}"})
        assert r.status_code == 409, f"归档后提交任务应 409, 实际 {r.status_code} {r.text[:200]}"
        r = e1.post(f"/api/projects/{project_id}/unarchive", user_id=e1.user_id)
        assert r.status_code == 200 and r.json()["project"]["status"] == "active"
        ok("归档后编辑/提交均 409, 撤销归档恢复 active")
    except Exception as exc:  # noqa: BLE001
        fail(f"归档流程失败: {exc}")
        return 1

    step("审计查询(管理员) -> 事件存在")
    try:
        a = Client()
        login(a, "admin", ADMIN_NEW_PASSWORD)
        r = a.get("/api/admin/audit", user_id=a.user_id,
                  params={"entity_type": "project", "entity_id": project_id})
        assert r.status_code == 200, f"审计查询失败: {r.status_code} {r.text[:300]}"
        items = r.json().get("items") or r.json().get("events") or []
        assert len(items) > 0, f"项目 {project_id} 无审计事件"
        actions = {it.get("action") for it in items}
        assert any(x in actions for x in ("project.created", "project.archived", "project.draft_updated")), \
            f"审计缺少关键动作: {actions}"
        ok(f"审计事件 {len(items)} 条, 动作: {sorted(actions)[:8]}")
    except Exception as exc:  # noqa: BLE001
        fail(f"审计查询失败: {exc}")
        return 1

    step("存储视图 + 健康端点")
    try:
        a = Client()
        login(a, "admin", ADMIN_NEW_PASSWORD)
        r = a.get("/api/admin/storage", user_id=a.user_id)
        assert r.status_code == 200, f"存储视图失败: {r.status_code} {r.text[:200]}"
        # STO-07: 单一 StorageStatusDto(objects/capacity 顶层字段)
        assert "objects" in r.json() and "capacity" in r.json()
        r = a.get("/api/admin/health", user_id=a.user_id)
        assert r.status_code == 200
        r = a.get("/api/healthz")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        r = a.get("/api/readyz")
        assert r.status_code == 200 and r.json()["status"] == "ok"
        ok("存储视图/运维健康/healthz/readyz 均正常")
    except Exception as exc:  # noqa: BLE001
        fail(f"存储视图/健康端点失败: {exc}")
        return 1

    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    passed = sum(1 for _, p, _ in results if p)
    for name, p, detail in results:
        print(f"[{'PASS' if p else 'FAIL'}] {name} | {detail}")
    print(f"总计: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""前后端契约冒烟脚本: 核对后端 API 形状与前端适配层(client.ts asItems/oneOf 等)一致。

运行(主机 docker 容器内已起 web:8080 → backend):
    docker exec -i ies_plan-backend-1 python - <<'PY' ... (或本机 python3 执行)
"""
import json
import os
import sys
import urllib.request

# 在 web 容器外执行时(如 backend 容器内)覆盖: SMOKE_BASE=http://localhost:8000
BASE = os.environ.get("SMOKE_BASE", "http://localhost:8080/api")
FAIL = []


def call(method, path, body=None, token=None, expect=200):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            return res.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        status, payload = e.code, (json.loads(raw) if raw else None)
        if status != expect:
            FAIL.append(f"{method} {path} -> {status} (期望 {expect}): {str(payload)[:200]}")
        return status, payload


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAIL.append(name)


def main():
    # 1) 登录(初始密码; 容忍先前已改密 → 初始密码预期 401)
    status, data = call("POST", "/auth/login", {"username": "admin", "password": "iesplan-admin-initial"}, expect=401)
    if status != 200:
        status, data = call("POST", "/auth/login", {"username": "admin", "password": "Iesplan-Admin#2026e2e"})
    check("auth.login → AuthResponse{token,user{role,force_password_change}}", status == 200 and "token" in (data or {}) and "user" in (data or {}) and data["user"].get("role") == "admin", str(status))
    if status != 200:
        print("无法登录, 中止"); sys.exit(1)
    token = data["token"]
    # 旧会话位移 → 新会话为 takeover_pending: 需先确认接管(RPD 3.3)
    if data.get("needs_takeover_confirm"):
        s2, d2 = call("POST", "/auth/confirm-takeover", {"token": token}, token)
        if s2 == 200 and "token" in (d2 or {}):
            token = d2["token"]
            check("auth.confirm-takeover → 新 token", True)
    if data["user"].get("force_password_change"):
        call("POST", "/auth/change-password", {"old_password": "iesplan-admin-initial", "new_password": "Iesplan-Admin#2026e2e"}, token)
        status, data = call("POST", "/auth/login", {"username": "admin", "password": "Iesplan-Admin#2026e2e"})
        token = data["token"]

    # 2) projects.list → {projects: [...]}
    s, body = call("GET", "/projects", token=token)
    check("projects.list → {projects:[...]}", s == 200 and isinstance(body.get("projects"), list), str(s))
    project_id = body["projects"][0]["id"] if body.get("projects") else None
    if project_id is None:
        s, body = call("POST", "/projects", {"name": "smoke-project", "currency": "CNY", "utc_offset_minutes": 480}, token, expect=201)
        project_id = body["project"]["id"]
        check("projects.create → {project,my_role}", "my_role" in body, str(body.keys()))

    # 3) project view → {project, draft{revision}, versions, my_role}
    s, body = call("GET", f"/projects/{project_id}", token=token)
    check("projects.get → {project,draft,versions,my_role}", s == 200 and "project" in body and "draft" in body and "my_role" in body, str(list(body.keys())))
    revision = body["draft"].get("revision")

    # 4) versions → {versions:[...]}
    s, body = call("GET", f"/projects/{project_id}/versions", token=token)
    check("projects.versions → {versions:[...]}", s == 200 and isinstance(body.get("versions"), list))

    # 5) config → {config, meta, version, status, updated_at}
    s, body = call("GET", f"/projects/{project_id}/config", token=token)
    check("config.get → {config{parameters,variables,objectives,constraints,algorithm{name}},meta,version,status}", s == 200 and "config" in body and "parameters" in body["config"] and "irr_floor" in body["config"] and "meta" in body, str(list(body.keys())))
    s, body = call("GET", f"/projects/{project_id}/config/default", token=token)
    check("config.default → {config,meta}", s == 200 and "config" in body, str(s))
    s, body = call("GET", "/registry/algorithms", token=token)
    check("registry.algorithms → {algorithms:[{algo_id,name_zh,help_topic}]}", s == 200 and isinstance(body.get("algorithms"), list) and "algo_id" in body["algorithms"][0] if body.get("algorithms") else True, str(list(body.keys())))
    s, body = call("POST", f"/projects/{project_id}/config/validate", {"config": (call("GET", f"/projects/{project_id}/config", token=token)[1])["config"]}, token)
    check("config.validate → {diagnostics,count}", s == 200 and isinstance(body.get("diagnostics"), list), str(s))

    # 6) model graph → {graph_id, devices, ports, connections, layout}
    s, body = call("GET", f"/projects/{project_id}/model", token=token)
    check("model.getGraph → {graph_id,devices,ports,connections,layout}", s == 200 and isinstance(body.get("devices"), list) and isinstance(body.get("connections"), list), str(list(body.keys())))
    s, body = call("GET", "/registry/device-types", token=token)
    check("registry.device-types → {items:[{type_id,parameters}]}", s == 200 and isinstance(body.get("items"), list) and "type_id" in body["items"][0], str(list(body.keys())))

    # 7) draft 命令批(updateDraft 形状: {expected_revision, commands:[{id,unit,type,payload}]})
    s, body = call("PUT", f"/projects/{project_id}/draft", {
        "expected_revision": revision,
        "commands": [{
            "id": "smoke-1", "project_id": project_id, "expected_revision": revision,
            "session": "browser", "unit": "model", "type": "model.upsert_device",
            "payload": {"name": "smoke-device", "device_type": body["items"][0]["type_id"] if isinstance(body.get("items"), list) and body.get("items") else "ies.device.pv", "kind": "new", "model_fidelity": "medium", "params": {}},
        }],
    }, token)
    check("draft.updateDraft → {revision,results}", s == 200 and "revision" in body, str(s) + str(body)[:150])

    # 8) tasks 域: 列表 → {items, next_cursor}
    s, body = call("GET", f"/projects/{project_id}/tasks?limit=5", token=token)
    check("tasks.list → {items,next_cursor}", s == 200 and isinstance(body.get("items"), list), str(list(body.keys())))

    # 9) validation.run → {report{status,blocks_submit,diagnostics}, stored}
    s, body = call("POST", f"/projects/{project_id}/validation/run", {}, token)
    check("validation.run → {report{blocks_submit,diagnostics},stored}", s == 200 and "report" in body and "blocks_submit" in body["report"] and isinstance(body["report"].get("diagnostics"), list), str(s))

    # 10) admin: audit → {items,next_cursor}; health → {status,version,liveness,readiness}
    s, body = call("GET", "/admin/audit?limit=5", token=token)
    check("admin.audit → {items,next_cursor}", s == 200 and isinstance(body.get("items"), list), str(list(body.keys())))
    s, body = call("GET", "/admin/health", token=token)
    check("admin.health → {status,version,liveness,readiness}", s == 200 and "liveness" in body and "readiness" in body, str(list(body.keys())))
    s, body = call("GET", "/admin/storage", token=token)
    check("admin.storage → {objects{count,total_bytes}}", s == 200 and "objects" in body, str(list(body.keys())))
    s, body = call("GET", "/auth/users", token=token)
    check("auth.users → {users:[UserOut]}", s == 200 and isinstance(body.get("users"), list) and "role" in body["users"][0], str(s))

    print()
    if FAIL:
        print(f"共 {len(FAIL)} 项未通过:")
        for f in FAIL:
            print("  -", f)
        sys.exit(1)
    print("全部形状核对通过")


if __name__ == "__main__":
    main()

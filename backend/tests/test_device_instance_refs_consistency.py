"""小型定向测试：证明项目查询返回的引用与装配生成的实例引用一致，包含中文设备名称。"""

from iesplan.assembly import annotate_device_refs, build_assembly_doc_from_content


def test_project_view_and_assembly_instance_refs_consistent():
    devices_raw = [
        {
            "name": "厂房屋顶光伏",
            "device_type": "ies.device.power_curve",
            "kind": "new",
            "params": {"installed_capacity_kw": 200},
        },
        {
            "name": "既有储能系统",
            "device_type": "ies.device.battery",
            "kind": "existing",
            "params": {"capacity_kwh": 500, "rated_power_kw": 250},
        },
        {
            "name": "grid_main",
            "device_type": "ies.device.grid_connection",
            "kind": "existing",
            "params": {"max_import_power_kw": 1000},
        },
    ]

    # 1. 项目查询返回的实例引用（lifecycle.py 中经装配公开门面生成）
    query_devices = annotate_device_refs(devices_raw)
    assert len(query_devices) == 3
    # 验证中文名称与 ASCII 权威引用
    assert query_devices[0]["display_name"] == "厂房屋顶光伏"
    assert query_devices[0]["ref"] == "dx0"
    assert query_devices[1]["display_name"] == "既有储能系统"
    assert query_devices[1]["ref"] == "dx1"
    assert query_devices[2]["display_name"] == "grid_main"
    assert query_devices[2]["ref"] == "grid_main"

    query_refs = [d["ref"] for d in query_devices]

    # 2. 装配从草稿内容生成的实例引用
    content = {
        "name": "test_project",
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2026,
            "scenario_mode": "single",
        },
        "model": {
            "devices": devices_raw,
            "connections": [],
        },
        "bindings": {},
    }
    build_res = build_assembly_doc_from_content(content)
    assert build_res.ok, [(d.code, d.params) for d in build_res.diagnostics if d.blocking]
    assembly_devices = build_res.doc["devices"]

    # 3. 严格断言：项目查询向前端公开的引用集合与装配生成的设备实例 ID 完全一致
    assert list(assembly_devices.keys()) == sorted(query_refs)
    for qd in query_devices:
        assert qd["ref"] in assembly_devices
        inst = assembly_devices[qd["ref"]]
        assert inst["definition"]["id"] == qd["device_type"]
        assert inst["asset_origin"] == qd["kind"]

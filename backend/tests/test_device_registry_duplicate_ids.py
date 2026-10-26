"""设备候选集合入口: 跨文件重复稳定 ID 拒绝与原子发布。

0.15 首切片定向回归: 同一 ``device.id`` 出现在两个候选文件时, 旧行为
按文件顺序静默后者覆盖前者, 与 ``modules/devices`` 的失败语义相悖,
也让注册表身份依赖磁盘顺序。经公开入口 (``load_all_devices`` /
``init_registry`` / ``get_registry``) 验证:

- 平铺与子目录布局的重复 ID 均整体拒绝 (SYS-CFG-001, 定位到两个文件);
- 失败不替换已发布的注册表集合 (原子发布语义)。
"""

from __future__ import annotations

from pathlib import Path

import pytest  # type: ignore[import-untyped]

from iesplan.core.errors import AppError
from iesplan.devices import get_registry, init_registry
from iesplan.devices.loader import load_all_devices

_DEVICE_TMPL = """\
schema: ies.device-model
schema_version: "2.2.0"
device:
  id: {device_id}
  names:
    zh-CN: 探针负荷
    en-US: Probe Load
properties:
  rated_demand_kw:
    value: 40
    unit: kW
    valid_range: {{minimum: 0, maximum: 1000}}
interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {{minimum: 0, maximum: null}}
equations:
  variables: {{}}
  relations:
    - id: serve
      expression: "electricity_in[t] = rated_demand_kw"
"""


def _write_device(path: Path, device_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEVICE_TMPL.format(device_id=device_id), encoding="utf-8")
    return path


def _diagnostics(exc: AppError) -> list[dict]:
    return [d for d in (exc.params.get("diagnostics") or []) if isinstance(d, dict)]


class TestDuplicateStableIdRejected:
    def test_flat_layout_duplicate_rejected_whole_set(self, tmp_path: Path):
        _write_device(tmp_path / "a.yaml", "acme.device.probe_load")
        _write_device(tmp_path / "b.yaml", "acme.device.probe_load")

        with pytest.raises(AppError) as excinfo:
            load_all_devices(tmp_path)
        err = excinfo.value
        assert err.code == "SYS-CFG-001"
        details = [str(d.get("params", {}).get("detail", "")) for d in _diagnostics(err)]
        assert any(
            "acme.device.probe_load" in d and "a.yaml" in d for d in details
        ), f"重复 ID 诊断应引用先定义文件: {details}"
        files = [str(d.get("params", {}).get("file", "")) for d in _diagnostics(err)]
        assert any(f.endswith("b.yaml") for f in files), files

    def test_subdir_layout_duplicate_rejected(self, tmp_path: Path):
        _write_device(tmp_path / "a.yaml", "acme.device.probe_load")
        _write_device(tmp_path / "vendor" / "b.yaml", "acme.device.probe_load")

        with pytest.raises(AppError) as excinfo:
            load_all_devices(tmp_path)
        assert excinfo.value.code == "SYS-CFG-001"


class TestAtomicPublishOnRejection:
    def test_failed_reload_keeps_published_set(self, tmp_path: Path):
        good = tmp_path / "good"
        _write_device(good / "one.yaml", "acme.device.probe_ok")
        published = init_registry(good)
        assert published.snapshot() == ["acme.device.probe_ok"]

        bad = tmp_path / "bad"
        _write_device(bad / "a.yaml", "acme.device.probe_dup")
        _write_device(bad / "b.yaml", "acme.device.probe_dup")
        with pytest.raises(AppError) as excinfo:
            init_registry(bad)
        assert excinfo.value.code == "SYS-CFG-001"
        # 失败不触碰已发布的注册表集合(原子发布: 候选先整体加载, 成功才替换)。
        assert get_registry().snapshot() == ["acme.device.probe_ok"]

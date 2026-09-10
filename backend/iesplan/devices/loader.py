"""设备目录发现与加载（ies.device-model 2.0.0）。

- 插件式：新增设备 = 放入 catalog/<id>.yaml，无需改代码；
- 受控加载：任一设备校验失败 → 整体拒绝加载；
- 诊断以 Diagnostic 列表输出，error 级拒载。

本模块是 2.0 目录的最小公开门面，只依赖 parser2/contracts2 与安全 YAML，
不依赖旧 pricing/csv/1.0 spec，不恢复兼容分支。
"""

from __future__ import annotations

from pathlib import Path

from iesplan.core import yamlmini as _yamlmini
from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SYS_CFG_INVALID,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError
from iesplan.devices.contracts2 import DeviceModelDocument
from iesplan.devices.parser2 import parse_device_model_v2

#: 内置设备目录（仓库内置）
DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent / "catalog"


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------


def _device_yaml_files(directory: Path) -> list[Path]:
    """目录内设备 yaml 文件（严格校验，确定性排序；意外文件整体拒载）。"""
    return sorted(Path(directory).glob("*.yaml"))


def _load_raw(path: Path) -> tuple[dict | None, list[Diagnostic]]:
    """安全加载 YAML 原始映射；失败返回诊断。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [
            make_diag(
                SYS_CFG_INVALID,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"file": str(path), "detail": str(exc)},
                location={"object_type": "device-model", "file": str(path)},
            )
        ]
    try:
        raw = _yamlmini.load(text)
    except _yamlmini.YamlParseError as exc:
        return None, [
            make_diag(
                SYS_CFG_INVALID,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"file": str(path), "detail": str(exc)},
                location={"object_type": "device-model", "file": str(path)},
            )
        ]
    if not isinstance(raw, dict):
        return None, [
            make_diag(
                SYS_CFG_INVALID,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"file": str(path), "detail": "YAML 顶层必须是 mapping"},
                location={"object_type": "device-model", "file": str(path)},
            )
        ]
    return raw, []


def discover_device_dirs(base_dir: Path) -> list[Path]:
    """扫描 base_dir 下含设备 yaml 的目录（平铺或子目录布局），确定性排序。"""
    base = Path(base_dir)
    yamls: list[Path] = _device_yaml_files(base)
    for sub in sorted(p for p in base.iterdir() if p.is_dir()):
        yamls.extend(_device_yaml_files(sub))
    dirs = {p.parent for p in yamls}
    # 按首个文件 device.id 排序，保证确定性；加载失败时回退到路径排序
    def _key(d: Path) -> tuple[str, str]:
        files = _device_yaml_files(d)
        if files:
            raw, _ = _load_raw(files[0])
            if isinstance(raw, dict):
                dev = raw.get("device") or {}
                if isinstance(dev, dict) and isinstance(dev.get("id"), str):
                    return (dev["id"], str(d))
        return ("", str(d))

    return sorted(dirs, key=_key)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_device_file(yaml_path: Path) -> list[Diagnostic]:
    """校验单台设备：结构 + 语义（2.0 合同）。"""
    raw, diags = _load_raw(yaml_path)
    if diags:
        return diags
    assert raw is not None
    result = parse_device_model_v2(raw, file=str(yaml_path))
    return result.diagnostics


def validate_device_dir(dir_path: Path) -> list[Diagnostic]:
    """校验一个设备目录（可含多台设备，平铺 catalog 布局）。"""
    diags: list[Diagnostic] = []
    for yaml_path in _device_yaml_files(Path(dir_path)):
        diags.extend(validate_device_file(yaml_path))
    return diags


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _diag_error(directory: Path, diags: list[Diagnostic]) -> AppError:
    """把诊断列表打包为 AppError（SYS-CFG-001）。"""
    return AppError(
        f"设备加载校验失败: {[d.code for d in diags if d.severity == SEVERITY_ERROR]}",
        code="SYS-CFG-001",
        message_key="ies.diag.store.config_invalid",
        params={
            "dir": str(directory),
            "diagnostics": [d.to_dict() for d in diags],
        },
    )


def load_device_file(yaml_path: Path) -> DeviceModelDocument:
    """加载单个设备文件（校验通过后返回不可变文档）。"""
    raw, diags = _load_raw(yaml_path)
    if diags:
        raise _diag_error(yaml_path.parent, diags)
    assert raw is not None
    result = parse_device_model_v2(raw, file=str(yaml_path))
    if result.diagnostics:
        raise _diag_error(yaml_path.parent, result.diagnostics)
    assert result.document is not None
    return result.document


def load_all_devices(base_dir: Path) -> list[DeviceModelDocument]:
    """加载目录下全部设备；任一设备校验失败 → 整体拒绝加载。

    返回按 device.id 排序的设备文档列表。
    """
    base = Path(base_dir)
    docs: list[DeviceModelDocument] = []
    all_diags: list[Diagnostic] = []
    for d in discover_device_dirs(base):
        for yaml_path in _device_yaml_files(d):
            ds = validate_device_file(yaml_path)
            all_diags.extend(ds)
            if not any(x.severity == SEVERITY_ERROR or x.blocking for x in ds):
                # 解析成功才追加
                raw, _ = _load_raw(yaml_path)
                assert raw is not None
                res = parse_device_model_v2(raw, file=str(yaml_path))
                if res.document is not None:
                    docs.append(res.document)
    if any(x.severity == SEVERITY_ERROR or x.blocking for x in all_diags):
        raise _diag_error(base, all_diags)
    return sorted(docs, key=lambda doc: doc.device.id if doc.device else "")

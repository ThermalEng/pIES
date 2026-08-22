"""设备目录发现、加载与联合校验(yaml + $price + csv + 模型文件)(02 §6.3;05 §7.6)。

- 插件式: 新增设备 = 放入 catalog/<id>.yaml(+同名 csv), 无需改代码;
- 受控加载: 任一设备校验失败 → 整体拒绝加载(与 core/registry.py 现状一致,
  失败即拒绝, 不部分生效);
- 错误以 Diagnostic 列表输出(错误定位到文件/字段), error 级拒载、warning 级放行。
"""

from __future__ import annotations

from pathlib import Path

from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SYS_CFG_INVALID,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError
from iesplan.devices import yamlmini as _yamlmini
from iesplan.devices.pricing import PriceBook, resolve_param_default
from iesplan.devices.profile import read_standard_csv, validate_series_csv
from iesplan.devices.spec import (
    DeviceYamlSpec,
    load_yaml,
    with_resolved_defaults,
)

#: 内置设备数据目录(仓库内置; 可由项目数据目录覆盖)
DEFAULT_CATALOG_DIR = Path(__file__).resolve().parent / "catalog"


# ---------------------------------------------------------------------------
# 目录发现
# ---------------------------------------------------------------------------


def _device_yaml_files(directory: Path) -> list[Path]:
    """目录内设备 yaml 文件(排除 prices.yaml, 确定性排序)。"""
    return sorted(p for p in Path(directory).glob("*.yaml") if p.stem != "prices")


def _yaml_type_id(path: Path) -> str:
    """读取 yaml 的 type_id(读失败返回空串, 用于排序不抛错)。"""
    try:
        raw = _yamlmini.load(path.read_text(encoding="utf-8"))
    except (OSError, _yamlmini.YamlParseError):
        return ""
    if isinstance(raw, dict) and isinstance(raw.get("type_id"), str):
        return raw["type_id"]
    return ""


def _min_type_id(directory: Path) -> str:
    ids = [_yaml_type_id(p) for p in _device_yaml_files(directory)]
    return min(ids) if ids else ""


def discover_device_dirs(base_dir: Path) -> list[Path]:
    """扫描 base_dir 下含设备 yaml 的目录(平铺或子目录布局), 按 type_id 排序(确定性)。"""
    base = Path(base_dir)
    yamls: list[Path] = _device_yaml_files(base)
    for sub in sorted(p for p in base.iterdir() if p.is_dir()):
        yamls.extend(_device_yaml_files(sub))
    dirs = {p.parent for p in yamls}
    return sorted(dirs, key=lambda d: (_min_type_id(d), str(d)))


# ---------------------------------------------------------------------------
# 联合校验
# ---------------------------------------------------------------------------


def _diag_from_error(exc: AppError, path: Path) -> Diagnostic:
    """AppError → 诊断(error 级, blocking)。"""
    return make_diag(
        exc.code or SYS_CFG_INVALID,
        severity=SEVERITY_ERROR,
        blocking=True,
        params={**exc.params, "file": str(path)},
        location={"object_type": "device", "object_id": "", "file": str(path)},
    )


def _check_cross_field(spec: DeviceYamlSpec, yaml_path: Path) -> list[Diagnostic]:
    """跨字段约束(roadmap 0.5.0):状态标志/方法标志/命令引用完整性。"""
    diags: list[Diagnostic] = []

    def _loc(field: str) -> dict:
        return {"object_type": "device", "object_id": spec.type_id, "field": field, "file": str(yaml_path)}

    def _err(field: str, detail: str) -> None:
        diags.append(
            make_diag(
                SYS_CFG_INVALID,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"device_id": spec.type_id, "field": field, "detail": detail},
                location=_loc(field),
            )
        )

    if spec.stateful and not spec.states:
        _err("states", "stateful 设备必须声明 states")
    if not spec.stateful and spec.states:
        _err("states", "states 仅允许出现在 stateful 设备")
    if not spec.ports:
        _err("ports", "必须声明至少一个端口")
    if not spec.parameters:
        _err("parameters", "必须声明至少一个参数")

    # model_commands: 每个 capability 必须有命令；值必须是 <command-id>@<exact-version>。
    commands = dict(spec.model_commands) if spec.model_commands else {}
    for cap in spec.capabilities:
        if cap not in commands:
            _err("model_commands", f"capability {cap!r} 缺少对应 model_command")
    for cap, ref in commands.items():
        if not isinstance(ref, str) or "@" not in ref:
            _err(
                "model_commands",
                f"命令引用必须为 <command-id>@<exact-version>: {ref!r}(capability {cap!r})",
            )
        elif "iesplan" in ref or ".py" in ref or "/" in ref:
            _err("model_commands", f"命令引用禁止函数/包/模块/宿主机路径: {ref!r}")

    if spec.model_method == "data_repeat":
        inputs = spec.time_series.get("inputs", ())
        if not any(s.required and s.period for s in inputs):
            _err("time_series", "data_repeat 设备 inputs 中至少一个 required 列需带 period")

    for st in spec.states:
        if st.initial_ref and st.initial_ref not in spec.parameters:
            _err("states", f"状态 {st.key!r} initial_ref 引用的参数不存在: {st.initial_ref!r}")
        if st.bounds:
            for bkey, ref in st.bounds.items():
                if ref not in spec.parameters:
                    _err("states", f"状态 {st.key!r} {bkey} 引用的参数不存在: {ref!r}")

    seen_ports: set[str] = set()
    for p in spec.ports:
        if p.name in seen_ports:
            _err("ports", f"端口名重复: {p.name!r}")
        seen_ports.add(p.name)
        if p.capacity_ref and p.capacity_ref not in spec.parameters:
            _err("ports", f"端口 {p.name!r} capacity_ref 引用的参数不存在: {p.capacity_ref!r}")
    return diags


def _validate_device_file(yaml_path: Path, book: PriceBook) -> list[Diagnostic]:
    """联合校验单台设备:yaml 结构 + $price 引用 + (如有)csv + 模型文件。"""
    diags: list[Diagnostic] = []
    try:
        spec = load_yaml(yaml_path)
    except AppError as exc:
        return [_diag_from_error(exc, yaml_path)]  # 结构失败: 其余检查无意义
    try:
        resolve_param_default(spec, book)
    except AppError as exc:
        diags.append(_diag_from_error(exc, yaml_path))
    diags.extend(_check_cross_field(spec, yaml_path))

    csv_path = yaml_path.with_suffix(".csv")
    if csv_path.exists():
        try:
            df = read_standard_csv(csv_path, spec)
        except AppError as exc:
            diags.append(_diag_from_error(exc, csv_path))
        else:
            diags.extend(validate_series_csv(df, spec))
    elif spec.model_method in ("data_repeat", "data_predict"):
        diags.append(
            make_diag(
                SYS_CFG_INVALID,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={
                    "device_id": spec.type_id,
                    "detail": f"{spec.model_method} 设备必须附带同名标准 csv",
                    "file": str(csv_path),
                },
                location={
                    "object_type": "device",
                    "object_id": spec.type_id,
                    "field": "csv",
                    "file": str(csv_path),
                },
            )
        )

    return diags


def validate_device_dir(dir_path: Path, book: PriceBook) -> list[Diagnostic]:
    """联合校验一个设备目录(可含多台设备, 平铺 catalog 布局):每台设备
    yaml 结构 + $price 引用 + (如有)csv + 模型文件; 返回 Diagnostic 列表。"""
    diags: list[Diagnostic] = []
    for yaml_path in _device_yaml_files(dir_path):
        diags.extend(_validate_device_file(yaml_path, book))
    return diags


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def _diag_error(directory: Path, diags: list[Diagnostic]) -> AppError:
    """把诊断列表打包为 AppError(SYS-CFG-001, 携带全部诊断)。"""
    return AppError(
        f"设备加载校验失败: {[d.code for d in diags if d.severity == SEVERITY_ERROR]}",
        code="SYS-CFG-001",
        message_key="ies.diag.store.config_invalid",
        params={
            "dir": str(directory),
            "diagnostics": [d.to_dict() for d in diags],
        },
    )


def _load_validated(yaml_path: Path, book: PriceBook) -> DeviceYamlSpec:
    """加载已通过校验的 yaml(结构解析 + 价格解析写入参数默认值)。"""
    spec = load_yaml(yaml_path)
    resolved = resolve_param_default(spec, book)
    return with_resolved_defaults(spec, resolved)


def load_device_type(dir_path: Path, book: PriceBook) -> DeviceYamlSpec:
    """加载单个设备目录(校验通过 + 价格解析完成后返回)。

    目录需恰好含一个设备 yaml(每设备一目录布局);平铺 catalog 布局请用 load_all_devices。
    失败抛 AppError 并携带全部诊断。
    """
    yamls = _device_yaml_files(dir_path)
    if len(yamls) != 1:
        raise AppError(
            f"设备目录需恰好含一个设备 yaml, 实际 {len(yamls)} 个: {dir_path}",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"dir": str(dir_path), "count": len(yamls)},
        )
    diags = _validate_device_file(yamls[0], book)
    errors = [d for d in diags if d.severity == SEVERITY_ERROR]
    if errors:
        raise _diag_error(dir_path, diags)
    return _load_validated(yamls[0], book)


def load_all_devices(base_dir: Path, book: PriceBook) -> list[DeviceYamlSpec]:
    """加载目录下全部设备;任一设备校验失败 → 整体拒绝加载(保持受控加载语义)。

    返回按 type_id 排序的设备规格列表($price: 已解析)。
    """
    base = Path(base_dir)
    specs: list[DeviceYamlSpec] = []
    all_diags: list[Diagnostic] = []
    for d in discover_device_dirs(base):
        for yaml_path in _device_yaml_files(d):
            ds = _validate_device_file(yaml_path, book)
            all_diags.extend(ds)
            if not any(x.severity == SEVERITY_ERROR for x in ds):
                specs.append(_load_validated(yaml_path, book))
    if any(x.severity == SEVERITY_ERROR for x in all_diags):
        raise _diag_error(base, all_diags)
    return sorted(specs, key=lambda s: s.type_id)

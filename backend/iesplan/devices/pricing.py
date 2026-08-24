"""统一价格事实源:PriceBook 加载与 ``$price:`` 引用解析(02 §5、§6.2;05 §7.8)。

prices.yaml 是"常用成本/价格/税收默认值"的唯一事实源;设备 yaml 参数默认值以
``$price:<点分路径>`` 引用,加载期解析为数值,键缺失即拒绝设备注册(SYS-CFG-001),
杜绝规划类回退"默认值不落库 → CAPEX 静默 0"问题(02 §2.7)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from iesplan.core import yamlmini
from iesplan.core.errors import AppError, NotFoundError
from iesplan.devices.spec import DeviceYamlSpec

#: ``$price:`` 引用前缀
PRICE_REF_PREFIX = "$price:"
#: 缺省价格文件:内置设备数据目录 catalog/prices.yaml
DEFAULT_PRICES_PATH = Path(__file__).resolve().parent / "catalog" / "prices.yaml"

_REQUIRED_SECTIONS = ("version", "currency", "energy_prices", "device_costs", "finance")


def _err(message: str, **params: object) -> AppError:
    return AppError(message, code="SYS-CFG-001", message_key="ies.diag.store.config_invalid", params=params)


@dataclass(frozen=True, slots=True)
class PriceBook:
    """价格事实源(02 §6.2;emissions/algorithm 为 05 §7.8 保留段)。"""

    version: str
    currency: str
    energy_prices: dict[str, object]
    device_costs: dict[str, dict[str, object]]
    finance: dict[str, float]
    emissions: dict[str, float] = field(default_factory=dict)
    algorithm: dict[str, dict[str, float]] = field(default_factory=dict)


def load_price_book(path: Path | None = None) -> PriceBook:
    """加载 prices.yaml;缺省路径为内置 catalog/prices.yaml。

    必需段缺失/类型非法抛 AppError(SYS-CFG-001)。
    """
    path = Path(path) if path is not None else DEFAULT_PRICES_PATH
    file = str(path)
    try:
        raw = yamlmini.load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _err("价格文件读取失败", file=file) from exc
    except yamlmini.YamlParseError as exc:
        raise _err("价格文件语法错误", file=file, line=exc.line, detail=str(exc)) from exc
    if not isinstance(raw, dict):
        raise _err("价格文件顶层必须为映射", file=file)
    missing = [k for k in _REQUIRED_SECTIONS if k not in raw]
    if missing:
        raise _err(f"价格文件缺少必需段: {missing}", file=file, sections=missing)
    version = raw["version"]
    currency = raw["currency"]
    if not isinstance(version, str) or not isinstance(currency, str):
        raise _err("version/currency 必须为字符串", file=file)
    return PriceBook(
        version=version,
        currency=currency,
        energy_prices=raw["energy_prices"],
        device_costs=raw["device_costs"],
        finance=raw["finance"],
        emissions=dict(raw.get("emissions") or {}),
        algorithm=dict(raw.get("algorithm") or {}),
    )


def get(book: PriceBook, dotted_key: str) -> object:
    """按点分路径取价格值('device_costs.pv.unit_invest_cost');缺失抛 NotFoundError。"""
    root: dict[str, object] = {
        "version": book.version,
        "currency": book.currency,
        "energy_prices": book.energy_prices,
        "device_costs": book.device_costs,
        "finance": book.finance,
        "emissions": book.emissions,
        "algorithm": book.algorithm,
    }
    node: object = root
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise NotFoundError(
                f"价格键不存在: {dotted_key}",
                params={"key": dotted_key},
                location={"object_type": "price_book", "object_id": book.version, "field": dotted_key},
            )
        node = node[part]
    return node


def resolve_param_default(spec: DeviceYamlSpec, book: PriceBook) -> dict[str, object]:
    """把 parameters 中所有 '$price:...' 字符串默认值解析为数值。

    解析失败(键缺失)抛 AppError(SYS-CFG-001)并携带键名, 拒绝该设备注册。
    返回 {参数名: 解析后的默认值}。
    """
    resolved: dict[str, object] = {}
    for name, p in spec.parameters.items():
        default = p.default
        if not (isinstance(default, str) and default.startswith(PRICE_REF_PREFIX)):
            continue
        key = default[len(PRICE_REF_PREFIX) :].strip()
        try:
            resolved[name] = get(book, key)
        except NotFoundError as exc:
            raise _err(
                f"设备 {spec.type_id} 参数 {name!r} 引用的价格键缺失: {key}",
                device_id=spec.type_id,
                param=name,
                price_key=key,
            ) from exc
    return resolved


def finance_defaults(book: PriceBook) -> dict[str, float]:
    """返回 finance 段(tax_rate/discount_rate/project_years/depreciation_years/irr_floor),
    供 financial.py / eval_run.py 注入(替代函数默认参数硬编码)。"""
    return dict(book.finance)


def algorithm_defaults(book: PriceBook) -> dict[str, dict[str, float]]:
    """返回 algorithm 段(算法容差默认值,05 §7.8), 供 engines/selector 读取。"""
    return {k: dict(v) for k, v in book.algorithm.items()}

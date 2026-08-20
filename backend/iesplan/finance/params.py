"""财务参数定义与来源(03-module-decoupling.md §7.2 finance/params.py)。

FinanceParams 为财务计算的唯一参数载体;finance_params_from_config 从
calc_config(parameters.economic_* / 顶层 irr_floor)与价格初始化文件 prices.yaml
的 finance 节合并取值,项目级显式参数优先,其次价格事实源,最后内置默认值。

价格事实源属设备初始化模块(1 层 devices, 02-device-init-module.md §5/§6.2:
prices.yaml finance 节 = tax_rate/discount_rate/project_years/depreciation_years/irr_floor);
该模块可能由并行 agent 实施,本模块以惰性导入 + 内置兜底默认值的方式保持独立可导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

#: prices.yaml finance 节默认值(02 §5 定案),devices 模块缺失时的兜底。
FALLBACK_PRICE_FINANCE: dict[str, float] = {
    "tax_rate": 0.25,
    "discount_rate": 0.08,
    "project_years": 20,
    "depreciation_years": 10,
    "irr_floor": 0.08,
}


@dataclass(frozen=True, slots=True)
class FinanceParams:
    """财务计算参数(03 §7.2 / 02 §5 finance 节)。

    属性:
        discount_rate: 贴现率(0..1)。
        tax_rate: 所得税率(0..1)。
        depreciation_years: 直线折旧年限(年)。
        project_years: 项目计算期(年)。
        currency: 币种(CNY/USD,仅记录展示,汇率走经济参数配置)。
        irr_floor: 最低税后 IRR 硬约束(0..1,REQ-CALC-006)。
    """

    discount_rate: Decimal = Decimal("0.08")
    tax_rate: Decimal = Decimal("0.25")
    depreciation_years: int = 10
    project_years: int = 20
    currency: str = "CNY"
    irr_floor: Decimal = Decimal("0.08")


# ---------------------------------------------------------------------------
# 来源解析
# ---------------------------------------------------------------------------


def finance_params_from_dict(mapping: dict) -> FinanceParams:
    """从字典(calc_config 经济段 / prices.yaml finance 节)构造 FinanceParams。

    键名:discount_rate/tax_rate/project_years/depreciation_years/currency/irr_floor;
    缺失键取 FinanceParams 默认值;数值统一经 Decimal 归一。
    """
    m = mapping or {}
    default = FinanceParams()
    return FinanceParams(
        discount_rate=Decimal(str(m.get("discount_rate", default.discount_rate))),
        tax_rate=Decimal(str(m.get("tax_rate", default.tax_rate))),
        depreciation_years=int(m.get("depreciation_years", default.depreciation_years)),
        project_years=int(m.get("project_years", default.project_years)),
        currency=str(m.get("currency", default.currency)),
        irr_floor=Decimal(str(m.get("irr_floor", default.irr_floor))),
    )


def _price_finance_defaults() -> dict[str, float]:
    """读取价格事实源 finance 节(惰性导入 devices 门面,失败回退内置默认)。

    回退只覆盖"模块不存在"的兼容场景(并行 agent 尚未落地 devices 门面):
    仅当 ``ModuleNotFoundError`` 且缺失模块就是目标模块时回退; 模块存在但
    加载失败(文件缺失/语法错误/段缺失或内部依赖错误)一律上抛
    (codex 二次审核 Medium-6: 不允许把实现错误误判为兼容缺失)。
    """
    from importlib import import_module

    for module_name in ("iesplan.devices.pricing", "iesplan.devices.prices"):
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            if getattr(exc, "name", None) == module_name:
                continue  # 兼容场景: 该模块路径未实现, 尝试下一个
            raise
        book = module.load_price_book()
        return dict(module.finance_defaults(book))
    return dict(FALLBACK_PRICE_FINANCE)


def finance_params_from_config(calc_config: dict | None) -> FinanceParams:
    """calc_config → FinanceParams(03 §7.2)。

    取值优先级:项目级 calc_config 显式参数 > prices.yaml finance 节 > 内置默认。
    calc_config 中经济参数位于 parameters.economic(存量格式)或 params(文档别名),
    二者兼容读取;irr_floor 为顶层独立字段(REQ-CALC-006,不得混入经济段)。
    """
    cfg = calc_config or {}
    merged: dict[str, float] = dict(_price_finance_defaults())

    econ = cfg.get("parameters", {})
    if not isinstance(econ, dict) or "economic" not in econ:
        econ = cfg.get("params", {})
    # economic 子层(存量格式)与扁平 params 层(文档别名)兼容读取:
    # 先取 economic 子层, 无则取扁平键(与 analysis.apply_param 的点路径语义一致)
    econ_block = econ.get("economic", {}) if isinstance(econ, dict) else {}
    if not isinstance(econ_block, dict) or not econ_block:
        econ_block = econ if isinstance(econ, dict) else {}
    for key in (
        "discount_rate",
        "tax_rate",
        "project_years",
        "depreciation_years",
        "currency",
    ):
        if key in econ_block and econ_block[key] is not None:
            merged[key] = econ_block[key]

    if cfg.get("irr_floor") is not None:
        merged["irr_floor"] = cfg["irr_floor"]

    return finance_params_from_dict(merged)

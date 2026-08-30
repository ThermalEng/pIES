"""finance 域领域规则层: FinanceConfig 领域校验与规划/财务 revision 一致性。

本模块消费 ``core.contracts.finance_config`` 的纯值对象, 补充 core 契约
不承载的**领域规则**:

- 币种一致性: 设备报价单位与能源价格单位必须与配置币种一致
  (单位前缀 ``<currency>/``, 如 CNY/kW);
- 公共边界复核: 财务配置只承载两阶段公共参数(核心契约已拦截未知字段),
  本层对必需公共参数的存在性给出领域诊断(供装配阶段聚合);
- 规划与财务同一 revision: ``check_finance_revision`` 强制
  PlanningConfig.finance_revision == FinanceConfig.revision(宪法 4.6),
  不一致产生 ``PROJ-PLAN-002`` 阻断诊断。

设备存在性/容量上下界落在设备技术区间等装配域校验(阶段 4 规划与财务
完整性)不在本层; 本层不依赖 HTTP、数据库或前端, 不反向依赖应用服务。
"""

from __future__ import annotations

from iesplan.core.contracts import FinanceConfig, MoneyAmount, PlanningConfig
from iesplan.core.diagnostics import Diagnostic, make_diag

#: 领域诊断码(登记于 core.diagnostics NEW_DIAG_CODES)。
FIN_CURRENCY_MISMATCH = "PROJ-FIN-002"
PLAN_FINANCE_REVISION_MISMATCH = "PROJ-PLAN-002"


def _money_unit_matches_currency(amount: MoneyAmount, currency: str) -> bool:
    """金额单位是否与配置币种一致(单位必须为 '<currency>/<量纲>' 形态)。"""
    return amount.unit.startswith(f"{currency}/")


def validate_finance_domain(config: FinanceConfig) -> list[Diagnostic]:
    """FinanceConfig 领域校验(币种一致性 + 公共参数完整性诊断)。

    返回阻断诊断列表(非法时非空; 核心契约已拦截的结构错误不在本层重复)。
    """
    diags: list[Diagnostic] = []
    for device_id, params in sorted(config.devices.items()):
        if not _money_unit_matches_currency(params.unit_investment, config.currency):
            diags.append(
                make_diag(
                    FIN_CURRENCY_MISMATCH,
                    params={
                        "detail": (
                            f"设备 {device_id} 单位建设投资单位 "
                            f"{params.unit_investment.unit!r} 与配置币种 "
                            f"{config.currency} 不一致"
                        ),
                    },
                    location={
                        "object_type": "finance_config",
                        "field": f"devices.{device_id}.unit_investment.unit",
                    },
                )
            )
        if not _money_unit_matches_currency(params.variable_om, config.currency):
            diags.append(
                make_diag(
                    FIN_CURRENCY_MISMATCH,
                    params={
                        "detail": (
                            f"设备 {device_id} 可变 O&M 单位 {params.variable_om.unit!r} "
                            f"与配置币种 {config.currency} 不一致"
                        ),
                    },
                    location={
                        "object_type": "finance_config",
                        "field": f"devices.{device_id}.variable_om.unit",
                    },
                )
            )
    for price_key, amount in sorted(config.energy_prices.items()):
        if not _money_unit_matches_currency(amount, config.currency):
            diags.append(
                make_diag(
                    FIN_CURRENCY_MISMATCH,
                    params={
                        "detail": (
                            f"能源价格 {price_key} 单位 {amount.unit!r} "
                            f"与配置币种 {config.currency} 不一致"
                        ),
                    },
                    location={
                        "object_type": "finance_config",
                        "field": f"energy_prices.{price_key}.unit",
                    },
                )
            )
    if not config.devices and not config.energy_prices:
        diags.append(
            make_diag(
                FIN_CURRENCY_MISMATCH,
                params={
                    "detail": "公共财务配置未声明任何设备条目或能源价格"
                    "(规划和财务计算共同参数缺失)",
                },
                location={"object_type": "finance_config", "field": "devices"},
            )
        )
    return diags


def check_finance_revision(
    planning: PlanningConfig, finance: FinanceConfig
) -> list[Diagnostic]:
    """强制规划与财务固定同一 FinanceConfig revision(宪法 4.6)。

    不一致返回 ``PROJ-PLAN-002`` 阻断诊断; 规划生成与结果财务计算只能
    消费同一 revision, 不允许各取一个。
    """
    if planning.finance_revision != finance.revision:
        return [
            make_diag(
                PLAN_FINANCE_REVISION_MISMATCH,
                params={
                    "detail": (
                        f"规划配置引用 finance_revision={planning.finance_revision}, "
                        f"财务配置实际 revision={finance.revision}"
                    ),
                },
                location={
                    "object_type": "planning_config",
                    "field": "finance_revision",
                },
            )
        ]
    return []

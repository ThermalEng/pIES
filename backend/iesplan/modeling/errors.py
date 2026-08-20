"""建模模块错误类型(码域 MOD-*)。

错误基类复用 0 层 ``iesplan.core.errors.AppError``(诊断码/严重度/文案键语义一致),
建模模块新增的码域以 ``MOD-`` 开头(与 ASM-*/DATA-* 等既有域并列)。
"""

from __future__ import annotations

from iesplan.core.errors import AppError


class ModelingError(AppError):
    """建模模块错误基类(码域 MOD-ERR-*)。"""

    code = "MOD-ERR-001"


class ModelingConfigError(ModelingError):
    """设备规格/命令配置非法(如未知 model_method、stateful 缺 states、$ref 白名单外)。

    对应 02 §3 校验约束与 03 §14.7 函数引用白名单;拒载语义:错误即拒绝注册,不静默降级。
    """

    code = "MOD-CFG-001"


class ModelingNotImplementedError(ModelingError):
    """数据-预测模型执行未实现(阶段 B 预留 stub 接口)。

    接口与输入校验已就绪,模型文件加载(onnx/joblib/pkl)在后续里程碑实现;
    调用 stub 时抛出,禁止静默降级为占位输出(05 §8.3 风险 3)。
    """

    code = "MOD-NOT-IMP-001"

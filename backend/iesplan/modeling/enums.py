"""建模模块枚举常量(05 §7.1 裁决:model_method/stateful 统一命名)。

模型方法取值按 05 架构总览 §7.1 裁决统一为 03 的 ``mechanism|data_repeat|data_predict``
(02 的 ``data_periodic/data_forecast``、04 的 ``model_kind/mechanistic`` 命名全部废止);
状态标志统一为 ``stateful: bool``(02 的 statefulness 枚举列不建)。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 模型方法(model_method)
# ---------------------------------------------------------------------------
MODEL_METHOD_MECHANISM = "mechanism"  # 机理模型:解析式/物理公式,绑定内置公式库
MODEL_METHOD_DATA_REPEAT = "data_repeat"  # 数据-周期重复:典型曲线按周期外推生成全年序列
MODEL_METHOD_DATA_PREDICT = "data_predict"  # 数据-预测:加载预测模型文件(onnx/查表/python)
MODEL_METHODS: tuple[str, ...] = (
    MODEL_METHOD_MECHANISM,
    MODEL_METHOD_DATA_REPEAT,
    MODEL_METHOD_DATA_PREDICT,
)

#: 模型精度档(02 §3:与 model_method 正交,只影响收敛/采样策略,不影响函数入口选择)
FIDELITY_VALUES: tuple[str, ...] = ("low", "medium", "high")

#: 机理函数引用白名单前缀(03 §14.7:yaml 只能引用 iesplan.modeling.functions.* 或已注册命令)
FUNCTION_REF_PREFIX = "iesplan.modeling.functions."

#: 标准化后台调用命令 id 前缀(03 §5.2:ies.command.model.<type>.<method>.<version>)
COMMAND_ID_PREFIX = "ies.command.model."

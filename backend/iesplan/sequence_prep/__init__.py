"""序列预备公开门面(0.6.5 事项 3)。

基于项目计算基线的确定性预备流程(roadmap 0.6.5 事项 3): 原始设备数据文件
允许不同采样间隔, 本模块按公开物理量语义把数据列重采样到基线分辨率,
``constant``/``data_repeat`` 确定性展开完整周期连续 ``step``,
``data_predict`` 固定使用 ``ies.predict.ridge@1.0.0`` 完成训练与全周期预测,
产出不可变计算用文件(``ies.device-data`` 2.0.0 ``prepared: true``)、训练产物
与变换回执; 任何校验失败结构化阻断, 不产出部分产物。

依赖方向: 本模块只消费 core(基线/诊断)与 devices 公开契约(设备文档与
ies.device-data 规范化器); 不访问数据库、对象存储、文件系统或 Worker。
事务式发布(替换模型实例数据引用、失败回滚)由 application/sequence_prep
用例编排, 见 modules/application.md「典型示例:预备项目计算序列」。
"""

from iesplan.sequence_prep.contracts import (
    QUANTITY_SEMANTICS,
    RECEIPT_SCHEMA,
    RECEIPT_SCHEMA_VERSION,
    RESAMPLE_RULES,
    SEMANTICS_CUMULATIVE,
    SEMANTICS_INSTANTANEOUS,
    SEMANTICS_STATE,
    DataRepeatSpec,
    PredictFile,
    PredictSpec,
    PreparedSequence,
    PrepOutcome,
)
from iesplan.sequence_prep.expand import expand_template, expected_template_rows, steps_per_day
from iesplan.sequence_prep.resample import resample_method, resample_series
from iesplan.sequence_prep.ridge import (
    ALGORITHM_ID,
    ALGORITHM_VERSION,
    ALPHA,
    SEED,
    artifact_bytes,
    artifact_sha256,
    predict_ridge,
    train_ridge,
)
from iesplan.sequence_prep.service import (
    prepare_constant,
    prepare_data_predict,
    prepare_data_repeat,
)

__all__ = [
    # 物理量语义与重采样规则(公共物理量登记表)
    "SEMANTICS_INSTANTANEOUS",
    "SEMANTICS_CUMULATIVE",
    "SEMANTICS_STATE",
    "QUANTITY_SEMANTICS",
    "RESAMPLE_RULES",
    # 周期展开
    "steps_per_day",
    "expected_template_rows",
    "expand_template",
    # 纯重采样引擎
    "resample_method",
    "resample_series",
    # ies.predict.ridge@1.0.0
    "ALGORITHM_ID",
    "ALGORITHM_VERSION",
    "ALPHA",
    "SEED",
    "train_ridge",
    "predict_ridge",
    "artifact_bytes",
    "artifact_sha256",
    # 预备服务与契约
    "prepare_constant",
    "prepare_data_repeat",
    "prepare_data_predict",
    "DataRepeatSpec",
    "PredictFile",
    "PredictSpec",
    "PreparedSequence",
    "PrepOutcome",
    "RECEIPT_SCHEMA",
    "RECEIPT_SCHEMA_VERSION",
]

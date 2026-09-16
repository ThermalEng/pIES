"""财务 YAML 文本出入(公开边界; 见 finance-yaml.md「规范化」)。

- ``effective_from_yaml``: 用户输入边界(YAML 文本 → 已签发生效快照)的
  唯一公开解析校验入口。字头与领域约束全部由三件套类型自身校验,
  调用方只映射异常, 不复制字段规则。
- ``effective_to_yaml``: 已签发快照 → 确定性 YAML 文本(稳定键序、金额
  定点十进制字符串、LF), 供签发时写入不可变内容对象。输入必须是已
  签发的 ``EffectiveFinanceConfig``(合并器产物), 本函数不做校验。
"""

from __future__ import annotations

from iesplan.core.yamlmini import load as _yaml_load
from iesplan.finance.triplet import EffectiveFinanceConfig


def effective_from_yaml(text: str) -> EffectiveFinanceConfig:
    """YAML 文本 → 不可变 EffectiveFinanceConfig(边界解析校验一次)。

    YAML 语法错误抛 ``YamlParseError``(core 公共类型); 字头/领域约束
    违反抛 ``FinanceTripletError``。调用方(装配 relative_file 边界、
    项目包导入边界)映射为各自诊断, 不得在本模块之外复制字段校验。
    """
    data = _yaml_load(text)
    return EffectiveFinanceConfig.from_dict(data)


def effective_to_yaml(config: EffectiveFinanceConfig) -> str:
    """已签发快照 → 确定性 YAML 文本(末尾 LF)。

    金额已在 ``to_dict`` 中规范为定点十进制字符串; 键序由 YAML
    序列化器稳定排序。相同快照永远得到相同字节。
    """
    from iesplan.core.yamlmini import dump as _yaml_dump

    return _yaml_dump(config.to_dict())


__all__ = ["effective_from_yaml", "effective_to_yaml"]

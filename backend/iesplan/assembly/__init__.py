"""装配域公共入口:ies.assembly 1.0.0 唯一生产管线。

生产调用(``application.tasks.submissions._assembly_gate``)只消费
``validator.validate_project_export`` 并持有其签发的
``ValidatedAssemblyArtifact``(规范文本 + 校验回执二件套);签发即受信,
不做重复复核。

管线:
- 手写文本 → ``parse_assembly_doc``(结构边界) → ``validate_assembly_text``;
- 项目内容 → ``build_assembly_doc_from_content``(构造边界) →
  ``validate_project_export``;
- 成功签发 ``ValidatedAssemblyArtifact``,失败返回结构化诊断,无可执行产物。

依赖方向:assembly → core(diagnostics/units/expression/yamlmini)+ devices
公开门面(设备描述符),不依赖 services/engines/worker。
"""

from iesplan.assembly.builder import BuildDocResult, build_assembly_doc_from_content
from iesplan.assembly.canonicalizer import (
    canonical_algorithm_ref,
    canonicalize_assembly_doc,
)
from iesplan.assembly.context import BusSummary, CheckContext
from iesplan.assembly.contracts import (
    ASSEMBLY_SCHEMA_PATH,
    CANON_ALGORITHM_ID,
    CANON_ALGORITHM_VERSION,
    SCHEMA_ID,
    SCHEMA_VERSION,
    VALIDATOR_ID,
    VALIDATOR_VERSION,
    AssemblyValidationError,
    ValidatedAssemblyArtifact,
    ValidationReceipt,
)
from iesplan.assembly.diags import ASM_ALL_CODES
from iesplan.assembly.parser import ParseDocResult, parse_assembly_doc
from iesplan.assembly.validator import (
    AssemblyValidationResult,
    validate_assembly_doc,
    validate_assembly_text,
    validate_project_export,
)

__all__ = [
    # ies.assembly 1.0.0 契约
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "ASSEMBLY_SCHEMA_PATH",
    "CANON_ALGORITHM_ID",
    "CANON_ALGORITHM_VERSION",
    "VALIDATOR_ID",
    "VALIDATOR_VERSION",
    "ValidationReceipt",
    "ValidatedAssemblyArtifact",
    "AssemblyValidationError",
    # 用户输入边界:文本结构解析 / 项目内容构造
    "parse_assembly_doc",
    "ParseDocResult",
    "build_assembly_doc_from_content",
    "BuildDocResult",
    # 校验入口与产物签发
    "AssemblyValidationResult",
    "validate_assembly_text",
    "validate_assembly_doc",
    "validate_project_export",
    # 规范化与共享上下文/诊断码
    "canonicalize_assembly_doc",
    "canonical_algorithm_ref",
    "CheckContext",
    "BusSummary",
    "ASM_ALL_CODES",
]

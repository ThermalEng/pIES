"""装配与检查模块(设计蓝图见开发者指南 architecture.md 与 contracts.md)。

边-端(edge-node)模型:节点 = 设备实例,端 = 端口,边 = 输出→输入链接
(两端参数同一时间步严格相等;损耗/延迟必须经管道设备建模,体现非同时性)。

公共 API:
- parse_assembly / load_assembly_file:文本 → AssemblySpec(阶段 A 语法/结构);
- build_assembly / dumps_assembly / build_assembly_text:项目图 → 规范文本(确定性);
- check_assembly / check_assembly_text / check_graph_inputs:旧格式四阶段检查(语法 A →
  连接合法性 B → 模型可解性 C → 整体可解性 D),输出结构化 ASM 域诊断;
- validate_assembly_text / validate_project_export:ies.assembly 1.0.0 唯一校验入口；
  计算任务只消费成功签发的 ValidatedAssemblyArtifact。

依赖方向:assembly → core(diagnostics/units/registry/expression)+ models(仅常量),
不依赖 services/engines/worker。
"""

from iesplan.assembly.builder import build_assembly, build_assembly_text, dumps_assembly
from iesplan.assembly.canonicalizer import (
    assembly_sha256,
    canonical_algorithm_ref,
    canonicalize_assembly_doc,
)
from iesplan.assembly.checker import (
    AssemblyCheckError,
    BusSummary,
    CheckContext,
    CheckResult,
    check_assembly,
    check_assembly_text,
    check_graph_inputs,
)
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
from iesplan.assembly.migration import (
    MigrationResult,
    migrate_assembly_spec,
    migrate_assembly_text,
)
from iesplan.assembly.parser import ParseResult, load_assembly_file, parse_assembly
from iesplan.assembly.parser10 import ParseDocResult, parse_assembly_doc
from iesplan.assembly.schema import FORMAT_VERSION, AssemblySpec
from iesplan.assembly.validator import (
    AssemblyValidationResult,
    validate_assembly_doc,
    validate_assembly_text,
    validate_project_export,
)
from iesplan.assembly.validator2 import (
    CANON2_ALGORITHM_ID,
    CANON2_ALGORITHM_VERSION,
    PREDEFINED_SOURCE_MODES,
    SCHEMA2_ID,
    SCHEMA2_VERSION,
    VALIDATOR2_ID,
    VALIDATOR2_VERSION,
    InterfaceNetworkResult,
    NetworkReceipt,
    ValidatedInterfaceNetwork,
    validate_interface_network2,
)

__all__ = [
    "parse_assembly",
    "load_assembly_file",
    "build_assembly",
    "dumps_assembly",
    "build_assembly_text",
    "check_assembly",
    "check_assembly_text",
    "check_graph_inputs",
    "CheckContext",
    "CheckResult",
    "BusSummary",
    "AssemblyCheckError",
    "AssemblySpec",
    "ParseResult",
    "ASM_ALL_CODES",
    "FORMAT_VERSION",
    # ies.assembly 1.0.0(roadmap 0.7.0)
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
    "canonicalize_assembly_doc",
    "assembly_sha256",
    "canonical_algorithm_ref",
    "parse_assembly_doc",
    "ParseDocResult",
    "AssemblyValidationResult",
    "validate_assembly_text",
    "validate_assembly_doc",
    "validate_project_export",
    "MigrationResult",
    "migrate_assembly_text",
    "migrate_assembly_spec",
    # 2.0 接口网络纯协议校验(ies.assembly 2.0.0 切片)
    "SCHEMA2_ID",
    "SCHEMA2_VERSION",
    "VALIDATOR2_ID",
    "VALIDATOR2_VERSION",
    "CANON2_ALGORITHM_ID",
    "CANON2_ALGORITHM_VERSION",
    "PREDEFINED_SOURCE_MODES",
    "NetworkReceipt",
    "ValidatedInterfaceNetwork",
    "InterfaceNetworkResult",
    "validate_interface_network2",
]

"""模板 inputs 深度合并与实例化器（`ies.device-model.template@2.0.0`）。

`inputs` 与模型使用同构树形结构：叶子节点是 `type` 声明（number/boolean/string/
data_repeat/data_predict），叶子路径对应模型中的具体字段：

- `properties.<id>.value`：标量叶子，替换或添加 property 的 `value`；
  添加新 property 时以叶子声明的 `unit`/`valid_range` 构造完整字段；
- `interfaces.<id>.source.value` / `source.data_ref`：constant 提交标量、
  data_repeat/data_predict 提交 `data_ref` 字符串，替换预定义来源；
- `equations.variables.<id>.initial.value`：替换内部变量初值。

实例化规则（格式标准「inputs 实例化」）：
1. 按 `mapping` 递归合并；
2. 标量和数组整体替换；
3. `inputs` 字段在模型中存在则覆盖，不存在则添加（property 添加由叶子声明补全字段）；
4. 用户只能提交 `inputs` 已声明字段，未声明字段拒绝；
5. 合并后删除 inputs，输出普通 2.0.0 模型；
6. 输出必须重新通过完整 2.0.0 校验，最终 schema 不允许的新增字段拒绝。

切片边界：本版本支持合并到已存在目标（覆盖）与添加 property；添加 interface 或
equation variable 需要 carrier/source 等额外声明，超出本切片范围，明确报错。
模板修改不改变已经生成的模型。保存模板摘要、输入摘要、实例化器版本与
最终模型摘要用于追溯（由调用方持久化，本模块只提供纯计算）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from iesplan.devices.contracts2 import (
    INPUT_DATA_TYPES,
    SCHEMA_ID,
    SCHEMA_VERSION,
    TemplateInputSpec,
)
from iesplan.devices.parser2 import ParseError, parse_device_model_v2, parse_template_inputs

INSTANTIATOR_VERSION = "ies.device-model.template@2.0.0"

#: 允许的叶子路径第一段 → 模型容器（用于“添加”场景与非法路径拒绝）
_ALLOWED_FIRST_SEGMENTS = ("properties", "interfaces", "equations")


@dataclass(slots=True)
class InstantiateResult:
    """模板实例化结果：最终模型文档 + 追溯摘要。"""

    document: Any  # DeviceModelDocument（解析后完整文档）
    canonical_text: str
    content_sha256: str
    template_sha256: str
    inputs_sha256: str
    receipt: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.document is not None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _diag_list(file: str, detail: str) -> list[Any]:
    from iesplan.core.diagnostics import make_diag

    return [
        make_diag(
            "SYS-CFG-001",
            severity="error",
            params={"file": file, "detail": detail},
            location={"object_type": "device-model", "file": file},
        )
    ]


def _no_inputs_diag(file: str) -> list[Any]:
    return _diag_list(file, "模板必须声明顶层 inputs")


def _check_submitted(node: Any, allowed: set[tuple[str, ...]], path: tuple[str, ...],
                     file: str, leaf_types: dict[tuple[str, ...], TemplateInputSpec]) -> None:
    """递归检查用户提交树：只允许模板已声明的路径前缀，未声明字段拒绝。

    叶子路径（声明终点）要求用户值为标量/数组（整体替换）；中间容器必须是 mapping。
    """
    if not isinstance(node, Mapping):
        raise ParseError(f"inputs.{'.'.join(path) or '<root>'} 必须是 mapping")
    for key, value in node.items():
        p = path + (key,)
        if p not in allowed:
            raise ParseError(f"inputs.{'.'.join(p)} 未在模板 inputs 中声明")
        leaf = leaf_types.get(p)
        if leaf is not None:
            # 叶子终点的值必须是标量/数组（整体替换）；mapping 值意味着用户把叶子当容器
            if isinstance(value, Mapping):
                raise ParseError(f"inputs.{'.'.join(p)} 必须是标量/数组值，不能是对象")
            continue
        if not isinstance(value, Mapping):
            raise ParseError(f"inputs.{'.'.join(p)} 必须是对象（该路径下还有子声明）")
        _check_submitted(value, allowed, p, file, leaf_types)


def _validate_leaf_value(spec: TemplateInputSpec, value: Any) -> None:
    """叶子值类型/范围校验（写入前）。"""
    if spec.type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParseError(f"inputs.{spec.path} 期望 number，收到 {type(value).__name__}")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ParseError(f"inputs.{spec.path} 必须为有限数值")
        if spec.valid_range is not None:
            lo, hi = spec.valid_range
            if lo is not None and v < lo:
                raise ParseError(f"inputs.{spec.path} 低于 valid_range.minimum {lo}")
            if hi is not None and v > hi:
                raise ParseError(f"inputs.{spec.path} 高于 valid_range.maximum {hi}")
    elif spec.type == "boolean":
        if not isinstance(value, bool):
            raise ParseError(f"inputs.{spec.path} 期望 boolean，收到 {type(value).__name__}")
    elif spec.type == "string":
        if not isinstance(value, str):
            raise ParseError(f"inputs.{spec.path} 期望 string，收到 {type(value).__name__}")
    elif spec.type in INPUT_DATA_TYPES:
        if not isinstance(value, str) or not value.strip():
            raise ParseError(f"inputs.{spec.path} 期望非空 data_ref 字符串")


def _validate_source_mode(leaf: TemplateInputSpec, model: Mapping[str, Any]) -> None:
    """data_repeat/data_predict 叶子要求模型目标 source.mode 与叶子类型一致。"""
    segs = leaf.path.split(".")
    node: Any = model
    try:
        for seg in segs[:-1]:
            node = node[seg]
    except (KeyError, TypeError):
        return  # 目标不存在：添加场景不检查
    if not isinstance(node, Mapping) or not isinstance(node.get("source"), Mapping):
        return
    mode = node["source"].get("mode")
    if mode is not None and mode != leaf.type:
        raise ParseError(
            f"inputs.{leaf.path} 声明 {leaf.type}，但模型 source.mode 为 {mode!r}"
        )


def _apply_leaf(leaf: TemplateInputSpec, model: dict[str, Any], user_value: Any) -> None:
    """把叶子值写入模型目标位置（覆盖或添加）。"""
    segs = leaf.path.split(".")
    target_segs, key = segs[:-1], segs[-1]
    node: dict[str, Any] = model
    for seg in target_segs:
        if not isinstance(node, dict):
            raise ParseError(f"inputs.{leaf.path} 合并目标不是 mapping")
        child = node.get(seg)
        if not isinstance(child, dict):
            child = {}
            node[seg] = child
        node = child
    # 添加 property 时补全字段（unit/valid_range 来自叶子声明）
    if len(target_segs) == 2 and target_segs[0] == "properties":
        if leaf.unit is not None:
            node.setdefault("unit", leaf.unit)
        else:
            # boolean/string 无量纲 property：schema 要求 unit 为字符串，使用 "-"
            node.setdefault("unit", "-")
        if leaf.valid_range is not None and "valid_range" not in node:
            node["valid_range"] = {"minimum": leaf.valid_range[0], "maximum": leaf.valid_range[1]}
    _validate_leaf_value(leaf, user_value)
    node[key] = user_value  # 在 _validate_leaf_value 之后写入：失败不落盘


def _merge_tree(leaves: tuple[TemplateInputSpec, ...], template_raw: Mapping[str, Any],
                inputs: Mapping[str, Any]) -> dict[str, Any]:
    """把用户 inputs 按叶子路径合并到模板模型副本中（覆盖或添加）。"""
    merged = json.loads(json.dumps(template_raw))  # 深拷贝为可变结构
    model_root: dict[str, Any] = merged
    for leaf in leaves:
        if leaf.path.split(".")[0] not in _ALLOWED_FIRST_SEGMENTS:
            raise ParseError(f"inputs.{leaf.path} 不是允许的合并目标（仅 properties/interfaces/equations 下）")
        if leaf.path.split(".")[0] in ("interfaces", "equations") and _is_add_target(leaf, model_root):
            raise ParseError(
                f"inputs.{leaf.path} 添加 interface/variable 不在本切片支持范围，请先在模板模型部分声明"
            )
        # 用户提交树中按叶子路径取用户值
        user_value: Any = inputs
        for seg in leaf.path.split("."):
            if not isinstance(user_value, Mapping) or seg not in user_value:
                user_value = None
                break
            user_value = user_value[seg]
        if user_value is None:
            continue  # 用户未提交该叶子：保持模型原样
        _validate_source_mode(leaf, model_root)
        _apply_leaf(leaf, model_root, user_value)
    return model_root


def _is_add_target(leaf: TemplateInputSpec, model: dict[str, Any]) -> bool:
    """叶子路径在模型中是否不存在（添加场景）。"""
    node: Any = model
    for seg in leaf.path.split("."):
        if not isinstance(node, Mapping) or seg not in node:
            return True
        node = node[seg]
    return False


def instantiate_template(
    template_raw: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    file: str = "",
) -> tuple[InstantiateResult | None, list[Any]]:
    """把模板原始映射 + 用户 inputs 实例化为普通 2.0.0 模型。

    返回 (result, diagnostics)：失败时 result 为 None，diagnostics 为诊断列表。
    """
    file = file or "<device-model-template>"

    # 1) 模板本身必须是合法模板（含顶层 inputs）
    template_result = parse_device_model_v2(template_raw, file=file)
    if not template_result.ok:
        return None, template_result.diagnostics
    template_doc = template_result.document
    assert template_doc is not None
    if template_doc.inputs is None:
        return None, _no_inputs_diag(file)
    template_inputs = parse_template_inputs(template_doc.inputs, file=file)

    # 2) 用户只能提交模板已声明字段；值形状必须与叶子声明一致
    try:
        leaf_types = {tuple(leaf.path.split(".")): leaf for leaf in template_inputs.leaves}
        allowed = set(leaf_types)
        # 容器路径 = 所有叶子路径的真前缀
        for leaf_path in list(allowed):
            for i in range(1, len(leaf_path)):
                allowed.add(leaf_path[:i])
        _check_submitted(inputs, allowed, (), file, leaf_types)
    except ParseError as exc:
        return None, _diag_list(file, str(exc))

    # 3) 递归合并（以模板声明树为准，覆盖/新增到模型）
    try:
        merged = _merge_tree(template_inputs.leaves, template_raw, inputs)
    except ParseError as exc:
        return None, _diag_list(file, str(exc))

    # 4) 删除顶层 inputs，输出普通 2.0.0 模型
    merged.pop("inputs", None)

    # 5) 最终模型必须重新通过完整 2.0.0 校验
    final_result = parse_device_model_v2(merged, file=file)
    if not final_result.ok:
        return None, final_result.diagnostics

    # 6) 摘要与回执
    from iesplan.devices.contracts2 import canonical_bytes

    final_doc = final_result.document
    assert final_doc is not None
    text = canonical_bytes(final_doc).decode("utf-8")
    template_text = json.dumps(dict(template_raw), ensure_ascii=False, sort_keys=True)
    inputs_text = json.dumps(dict(inputs), ensure_ascii=False, sort_keys=True)
    return (
        InstantiateResult(
            document=final_doc,
            canonical_text=text,
            content_sha256=_sha256_text(text),
            template_sha256=_sha256_text(template_text),
            inputs_sha256=_sha256_text(inputs_text),
            receipt={
                "instantiator": INSTANTIATOR_VERSION,
                "schema": SCHEMA_ID,
                "schema_version": SCHEMA_VERSION,
                "content_sha256": _sha256_text(text),
                "template_sha256": _sha256_text(template_text),
                "inputs_sha256": _sha256_text(inputs_text),
            },
        ),
        [],
    )

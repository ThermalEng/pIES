"""
JSON 工具:递归安全化 + 规范化序列化(内容哈希的稳定基底)。

内容寻址对象(content-addressed objects)的 sha256 由 canonical_json 产出,
任何写入方都必须走同一实现, 否则相同内容的哈希会漂移(dedup/校验/可复现性破坏)。

覆盖各域的需求:
- datetime → ISO 字符串;
- Decimal → float(哈希口径; worker 中保精度场景显式用 str 转换后再传入);
- bytes → UTF-8 字符串(替换非法字节);
- numpy 数组/标量 → list/float;
- 其余未知类型 → str(兜底, 与 results/executors 旧行为一致)。
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

try:  # numpy 为可选依赖(未安装时仅影响 ndarray 分支)
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore


def jsonable(value: Any) -> Any:
    """递归转换为 JSON 安全值。"""
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if np is not None and isinstance(value, np.ndarray):
        return jsonable(value.tolist())
    if np is not None and isinstance(value, np.generic):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def canonical_json(content: Any) -> str:
    """规范化 JSON(键排序、紧凑分隔), 保证相同内容的哈希稳定。"""
    return json.dumps(jsonable(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

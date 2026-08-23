"""GUI 上传的设备描述解析(0.6.0: 上传与包内 CSV 共用同一规范化流程)。

上传文件两类:
- 声明 ``ies.device-data`` 元数据 → 按文件头 device_model(`<id>@<version>`)
  解析目录设备描述; 精确版本不匹配由规范化器给出 DATA-META-008;
- 裸 CSV → 调用方提供的 fallback 描述符(services 层从自身 STANDARD_FIELDS
  权威构建, 本模块不复制任何列/单位映射)。

本模块只依赖 core/errors 与 datacontract, 不导入 services。
"""

from __future__ import annotations

from dataclasses import dataclass

from iesplan.devices.datacontract import DataInputDecl, declared_device_model, is_ies_device_data


@dataclass(frozen=True, slots=True)
class UploadDescriptor:
    """上传校验用的最小公开设备描述(与 DeviceModelDescriptor 消费形态一致)。"""

    type_id: str
    version: str
    data_inputs: dict[str, DataInputDecl]

    @property
    def device_model_id(self) -> str:
        return f"{self.type_id}@{self.version}"


def resolve_upload_descriptor(data: bytes, fallback_desc):
    """按上传字节解析校验用描述符。

    - 声明 ies.device-data → 目录设备描述(精确版本由规范化器比对
      DATA-META-008); 文件头 device_model 无法解析或未注册 → 抛 LookupError,
      阻断上传, 不猜测;
    - 裸 CSV → fallback_desc(调用方权威)。
    """
    if not is_ies_device_data(data):
        return fallback_desc
    model = declared_device_model(data)
    type_id = model.split("@", 1)[0] if model else ""
    if not type_id:
        raise LookupError(f"文件声明 ies.device-data 但缺少 device_model: {model!r}")
    from iesplan.core.errors import NotFoundError
    from iesplan.devices import get_device_descriptor

    try:
        return get_device_descriptor(type_id)
    except NotFoundError as exc:
        # 未注册设备模型是上传内容校验失败(400 阻断), 不是资源不存在(404);
        # 调用方(数据服务)转成阻断性诊断。
        raise LookupError(f"文件声明的设备模型未注册: {model!r}") from exc


def upload_declared_units(desc, fields: dict | None) -> dict[str, str]:
    """上传 fields 声明 + 描述符权威单位 → unit.<column> 元数据(裸 CSV 用)。

    fields 显式给出的单位必须与描述符量纲兼容, 不兼容交由规范化器
    (DATA-COL-006)阻断; 本函数只合成元数据, 不复制换算规则。
    """
    units: dict[str, str] = {}
    for col, decl in (getattr(desc, "data_inputs", None) or {}).items():
        stated = ""
        raw = dict(fields or {}).get(col)
        if isinstance(raw, dict):
            stated = str(raw.get("unit", "") or "").strip()
        elif isinstance(raw, str):
            stated = raw.strip()
        units[col] = stated or decl.unit
    return units


__all__ = [
    "UploadDescriptor",
    "resolve_upload_descriptor",
    "upload_declared_units",
]

"""用户自定义模型模板用例(切片 dm2)。

对应 customization-center.md「编写设备模型」与格式标准「模板(未实例化阶段)」:
创建草稿 / 列表 / 详情 / 乐观锁更新 / 完整校验 / 发布不可变 revision /
停用启用 / 删除未发布草稿 / 项目模板目录。公开门面只导出用例命令与值对象。
"""

from iesplan.application.model_templates.service import (
    TEMPLATE_OWNER_NAMESPACE,
    TemplateNotFoundError,
    TemplateValidationError,
    create_template_draft,
    delete_template_draft,
    get_template_detail,
    get_template_revision,
    list_available_templates,
    list_my_templates,
    publish_template,
    resolve_template_revision,
    save_template_draft,
    set_template_status,
    validate_template_revision,
    validate_template_yaml,
)

__all__ = [
    "TEMPLATE_OWNER_NAMESPACE",
    "TemplateNotFoundError",
    "TemplateValidationError",
    "create_template_draft",
    "delete_template_draft",
    "get_template_detail",
    "get_template_revision",
    "list_available_templates",
    "list_my_templates",
    "publish_template",
    "resolve_template_revision",
    "save_template_draft",
    "set_template_status",
    "validate_template_revision",
    "validate_template_yaml",
]

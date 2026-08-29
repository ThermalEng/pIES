"""成功响应包装键集中登记表(0.3.0 ADR-0005; 对应错误信封 NEW_DIAG_CODES 的角色)。

宪法 §8.2 / contracts.md「成功与错误」: 成功响应使用一级命名键包装,
每个键名直接表达资源语义。本表是全库唯一权威登记 —— 每个路由端点
的允许顶层键集必须在此登记, 否则协议基线测试(test_protocol_baseline.py
test_wrapper_keys_registered)即失败。

登记规则:
- 键 = 路由端点函数名(与 FastAPI 装饰器挂载的函数一致, 不依赖路由路径
  前缀拼接, 避免前缀变化导致键表失效);
- 值 = 该端点允许的顶层键集(多分支登记并集);
- 间接返回(return 服务调用结果)的端点也必须登记 —— 键集从服务层返回值
  提取(登记时注明来源函数), 服务层形状变化会在此暴露;
- 新增/修改端点: 先改本表再改代码, 保持两者同步;
- 嵌套键(如 {project, draft, versions, my_role})只登记顶层键, 嵌套结构
  由服务层序列化约定约束。

列表端点约定: {items, total} 或 {items, next_cursor} 二键(带分页) /
  {<resource_name>}(不带分页, 如 {projects} / {datasets} / {versions})。
动作响应: {ok, ...} / {message_key, ok} 等。

非 JSON 响应端点(302 重定向 / CSV / excel / package 二进制下载)不登记,
由协议基线测试显式豁免(见 test_protocol_baseline.py test_wrapper_keys_registered)。
"""

#: 路由端点函数名 → 允许的顶层键集(并集)
WRAPPER_KEYS: dict[str, frozenset[str]] = {
    # ---- admin.py ----
    "query_audit_endpoint": frozenset({"items", "next_cursor"}),  # services.audit.query_audit
    "diagnostics_endpoint": frozenset(
        {"healthy", "maintenance_actions", "queue", "retention_rules", "storage", "tasks"}
    ),
    "unlock_task_endpoint": frozenset({"status", "task_id", "unlocked", "message"}),
    # ---- auth.py ----
    "login": frozenset({"token", "token_type", "user", "needs_takeover_confirm"}),  # AuthResponse
    "logout": frozenset({"ok"}),
    "change_password": frozenset({"message_key", "ok"}),
    "refresh": frozenset({"expires_at", "ok"}),
    "me": frozenset({"user"}),
    "confirm_takeover": frozenset({"ok", "user"}),
    "register": frozenset({"user"}),
    "list_users": frozenset({"users"}),
    "admin_reset_password": frozenset({"message_key", "ok"}),
    "admin_deactivate_user": frozenset({"ok"}),
    "admin_reactivate_user": frozenset({"ok"}),
    "admin_delete_user_preview": frozenset(
        {"confirm_token", "project_count", "projects", "user_id", "username"}
    ),  # services.identity.preview_user_delete
    "admin_delete_user": frozenset({"ok", "deleted_projects"}),  # services.identity.delete_user
    "update_settings": frozenset({"registration_enabled"}),
    "read_settings": frozenset({"registration_enabled"}),
    "get_public_settings": frozenset({"registration_enabled"}),
    # ---- config.py ----
    "get_config_endpoint": frozenset(
        {"config", "meta", "status", "updated_at", "version"}
    ),  # services.config._read_config
    "save_config_endpoint": frozenset(
        {"config", "meta", "status", "updated_at", "version", "diagnostics"}
    ),
    "validate_config_endpoint": frozenset({"count", "diagnostics"}),
    "default_config_endpoint": frozenset({"config", "meta"}),
    "algorithms_endpoint": frozenset({"algorithms"}),
    # ---- datasets.py ----
    "create_dataset": frozenset({"dataset"}),
    "list_datasets": frozenset({"datasets"}),
    "get_dataset": frozenset({"dataset", "versions"}),
    "upload_version": frozenset({"dataset_version", "diagnostics", "quality_report"}),
    "get_version": frozenset({"data", "dataset_version", "files", "license", "provenance"}),
    "create_sample": frozenset({"dataset_version", "quality_report"}),
    # ---- exports.py ----
    "export_excel_endpoint": frozenset(
        {"expires_at_seconds", "file_name", "sha256", "size_bytes", "token"}
    ),
    "export_package_endpoint": frozenset(
        {"expires_at", "file_name", "manifest", "media_type", "object_id", "oid", "sha256", "size_bytes", "token"}
    ),  # services.package.PackageExport.to_dict
    # ---- health.py ----
    "admin_health": frozenset({"components", "status"}),
    # ---- model.py ----
    "device_types_public": frozenset({"items"}),
    "get_model_graph": frozenset(
        {"has_graph", "graph_id", "name", "graph_hash", "devices", "ports", "connections", "layout"}
    ),  # services.model.get_graph
    "create_device": frozenset({"device", "ports"}),
    "update_device": frozenset({"device"}),
    "delete_device": frozenset({"deleted", "ok"}),
    "create_connection": frozenset({"connection"}),
    "update_connection": frozenset({"connection"}),
    "delete_connection": frozenset({"deleted", "ok"}),
    "validate_model": frozenset({"diagnostics"}),
    # ---- objects.py ----
    "admin_storage": frozenset(
        {"capacity", "cleanup_candidates", "corrupt_count", "healthy", "objects", "pending_deletion_count", "refs"}
    ),
    "admin_cleanup": frozenset({"cleaned", "count", "dry_run"}),
    "admin_pending_objects": frozenset({"data", "meta"}),
    "admin_restore_object": frozenset({"data", "meta"}),
    "admin_purge_objects": frozenset({"purged_count", "dry_run"}),
    "admin_storage_health": frozenset(
        {"capacity", "corrupt_count", "object_count", "ok", "orphan_count", "pending_deletion_count", "reconcile"}
    ),
    # ---- projects.py ----
    "create_project_endpoint": frozenset({"my_role", "project"}),
    "list_projects_endpoint": frozenset({"projects"}),
    "list_all_projects_endpoint": frozenset({"projects"}),
    "get_project_endpoint": frozenset(
        {"project", "draft", "versions", "my_role"}
    ),  # services.project.get_project_view
    "update_draft_endpoint": frozenset({"results", "revision"}),  # services.project.update_draft
    "create_version_endpoint": frozenset({"version"}),
    "list_versions_endpoint": frozenset({"versions"}),
    "get_version_endpoint": frozenset({"version"}),
    "restore_version_endpoint": frozenset({"draft", "version"}),  # services.project.restore_version
    "apply_result_endpoint": frozenset({"draft", "version"}),  # services.project.apply_result
    "archive_project_endpoint": frozenset({"my_role", "project"}),
    "unarchive_project_endpoint": frozenset({"my_role", "project"}),
    "delete_project_endpoint": frozenset({"deleted", "ok"}),
    "import_package_endpoint": frozenset({"proposal"}),
    "confirm_import_endpoint": frozenset({"my_role", "project"}),
    # ---- project_models.py (切片 dm2-A: 候选门禁与原子保存) ----
    "validate_project_model_candidate": frozenset({"diagnostics", "valid"}),
    "upload_project_model_temp_file": frozenset({"temp_file", "upload_id"}),
    "list_project_models_endpoint": frozenset({"project_models"}),
    "save_project_model_endpoint": frozenset(
        {"duplicate", "project_model", "receipt"}
    ),  # 幂等重放时 duplicate 存在, 登记并集
    "delete_project_model_endpoint": frozenset({"deleted", "ok"}),
    # ---- results.py ----
    "get_result_endpoint": frozenset({"result"}),  # services.results.result_view
    "list_assessments_endpoint": frozenset({"items", "total"}),
    "assess_endpoint": frozenset({"assessment"}),
    "select_result_endpoint": frozenset({"diff", "selection"}),
    "diff_endpoint": frozenset({"diff"}),
    "hourly_endpoint": frozenset(
        {"end", "field", "next_start", "start", "total_rows", "unit", "values"}
    ),  # services.results.read_hourly
    "check_task_endpoint": frozenset({"task"}),
    # ---- tasks.py ----
    "create_task_endpoint": frozenset({"duplicate", "hint", "replayed", "task"}),
    "list_tasks_endpoint": frozenset({"items", "next_cursor"}),  # services.tasks.list_tasks
    "get_task_endpoint": frozenset({"task"}),
    "cancel_task_endpoint": frozenset({"cancel_status", "diagnostic", "task"}),
    "retry_task_endpoint": frozenset({"task"}),
    # ---- validation.py ----
    "run_validation": frozenset({"report", "stored"}),
    "baseline_confirm": frozenset({"assumptions_hash", "confirmed", "confirmed_at", "confirmed_by"}),
    "get_validation_report": frozenset({"report", "stored"}),
}

"""本地生态算法插件包上传、安全解压、受控构建、断网双模式自检、审批与并发安全卸载用例。"""

from __future__ import annotations

import io
import json
import re
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError
from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import ecosystem as ecosystem_domain
from iesplan import tasks as tasks_domain
from iesplan.application.ecosystem.common import (
    initial_approval_status,
    is_admin_user,
    require_admin,
    serialize_artifact,
    validate_artifact_stable_id,
    verify_external_sha256,
)
from iesplan.computation.contracts.compatibility import (
    SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS,
)
from iesplan.computation.runtime.job_runner_client import (
    JobRunnerClientError,
    JobRunnerConflictError,
    StartStageSpec,
)
from iesplan.config import settings as app_settings
from iesplan.core.errors import AppError
from iesplan.core.yamlmini import dump as _yaml_dump
from iesplan.core.yamlmini import load as _yaml_load
from iesplan.ecosystem import (
    PLUGIN_KINDS,
    SOURCE_ORIGINS,
    EcosystemArtifactNotFoundError,
    EcosystemConflictError,
    EcosystemUninstallInUseError,
    EcosystemValidationError,
    LocalArtifactRecord,
)
from iesplan.identity.contracts import UserRecord
from iesplan.storage import attach, get_object, put_object

PLUGIN_ARCHIVE_MEDIA_TYPE = "application/zip"
_MAX_ZIP_BYTES = 50 * 1024 * 1024
_MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
_MAX_ZIP_ENTRIES = 1000
_FORBIDDEN_FILES = frozenset({"Dockerfile", ".dockerignore"})
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ENTRYPOINT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")
_TERMINAL_STAGE_STATUSES = frozenset({"exited", "dead"})
_DEFAULT_SINGLE_OUTPUT_FILES: dict[str, str] = {
    "predictor": "prediction.json",
    "generator": "bundle/bundle.yaml",
    "solver": "primal_solution.json",
    "evaluator": "evaluation_report.json",
    "algorithm": "solution.json",
}


def _default_plugin_source_root() -> Path:
    return Path(app_settings.data_dir) / "plugin_builds"


def _default_stage_root() -> Path:
    return Path(app_settings.stage_root)


def _normalize_zip_rel(raw_name: str, *, is_dir: bool) -> str | None:
    if not raw_name or "\\" in raw_name or raw_name.startswith("/"):
        raise EcosystemValidationError(
            f"ZIP 条目路径非法: {raw_name!r}", params={"entry": raw_name}
        )
    trimmed = raw_name.rstrip("/") if is_dir else raw_name
    if trimmed in ("", "."):
        if is_dir:
            return None
        raise EcosystemValidationError(f"ZIP 条目路径非法: {raw_name!r}")
    parts = [p for p in trimmed.split("/") if p != "."]
    if not parts or any(p in ("", "..") for p in parts):
        raise EcosystemValidationError(
            f"ZIP 条目路径越权: {raw_name!r}", params={"entry": raw_name}
        )
    rel = "/".join(parts)
    if rel in _FORBIDDEN_FILES or parts[-1] in _FORBIDDEN_FILES:
        raise EcosystemValidationError(
            f"插件包禁止自带 {parts[-1]}", params={"entry": raw_name}
        )
    return rel


def _inspect_plugin_zip(zip_bytes: bytes) -> tuple[dict[str, Any], dict[str, bytes], set[str]]:
    """安全检查算法插件 ZIP 并解析校验 plugin.yaml。"""
    if not zip_bytes:
        raise EcosystemValidationError("插件 ZIP 文件为空")
    if len(zip_bytes) > _MAX_ZIP_BYTES:
        raise EcosystemValidationError("插件 ZIP 超出最大体积限制")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r")
    except zipfile.BadZipFile as exc:
        raise EcosystemValidationError(f"无效的插件 ZIP 归档: {exc}") from exc

    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    seen_lower: dict[str, str] = {}
    total_uncompressed = 0

    with zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise EcosystemValidationError("插件 ZIP 条目数超出上限")
        for info in infos:
            if info.flag_bits & 0x1:
                raise EcosystemValidationError(f"禁止加密 ZIP 条目: {info.filename!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise EcosystemValidationError(f"插件 ZIP 禁止包含符号链接: {info.filename!r}")
            is_dir = info.is_dir() or info.filename.endswith("/")
            rel = _normalize_zip_rel(info.filename, is_dir=is_dir)
            if rel is None:
                continue
            low = rel.lower()
            prev = seen_lower.get(low)
            if prev is not None and not (is_dir and prev == rel):
                raise EcosystemValidationError(
                    f"插件 ZIP 存在路径或大小写碰撞: {prev!r} vs {rel!r}"
                )
            seen_lower[low] = rel
            if is_dir:
                dirs.add(rel)
                continue
            if (
                info.file_size > _MAX_UNZIPPED_BYTES
                or total_uncompressed + info.file_size > _MAX_UNZIPPED_BYTES
            ):
                raise EcosystemValidationError("插件 ZIP 解压总大小超出上限")
            data = zf.read(info.filename)
            total_uncompressed += len(data)
            if total_uncompressed > _MAX_UNZIPPED_BYTES:
                raise EcosystemValidationError("插件 ZIP 解压总大小超出上限")
            files[rel] = data

    if "plugin.yaml" not in files:
        raise EcosystemValidationError("插件 ZIP 根目录缺少 plugin.yaml")
    try:
        manifest = _yaml_load(files["plugin.yaml"].decode("utf-8"))
    except Exception as exc:
        raise EcosystemValidationError(f"plugin.yaml 解析失败: {exc}") from exc
    if not isinstance(manifest, dict):
        raise EcosystemValidationError("plugin.yaml 顶层须为对象")

    _validate_manifest_dict(manifest, files=files, dirs=dirs)
    return manifest, files, dirs


def _validate_manifest_dict(
    manifest: Mapping[str, Any],
    *,
    files: Mapping[str, bytes],
    dirs: set[str],
) -> None:
    if manifest.get("schema") != "ies.algorithm-plugin" or manifest.get("schema_version") != "1.0.0":
        raise EcosystemValidationError(
            "plugin.yaml 须声明 schema: ies.algorithm-plugin 与 schema_version: '1.0.0'"
        )
    kind = manifest.get("kind")
    if kind not in PLUGIN_KINDS:
        raise EcosystemValidationError(f"plugin.yaml kind 非法: {kind!r}")
    plugin_id = manifest.get("plugin_id")
    validate_artifact_stable_id(str(plugin_id or ""), expected_kind=str(kind))
    version = manifest.get("version")
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise EcosystemValidationError(f"plugin.yaml version 须为三段式语义版本: {version!r}")

    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or not _ENTRYPOINT_RE.fullmatch(entrypoint):
        raise EcosystemValidationError(f"plugin.yaml entrypoint 格式非法: {entrypoint!r}")
    mod_part, func_part = entrypoint.split(":", 1)
    if func_part.startswith("_"):
        raise EcosystemValidationError(f"entrypoint 函数不可为私有符号: {entrypoint!r}")
    mod_rel = mod_part.replace(".", "/")
    if f"{mod_rel}.py" not in files and f"{mod_rel}/__init__.py" not in files:
        if f"src/{mod_rel}.py" not in files and f"src/{mod_rel}/__init__.py" not in files:
            raise EcosystemValidationError(
                f"entrypoint 指向的模块文件不存在于插件包内: {mod_part!r}"
            )

    consumes = manifest.get("consumes")
    produces = manifest.get("produces")
    if not isinstance(consumes, list) or not consumes or not all(isinstance(x, str) and x for x in consumes):
        raise EcosystemValidationError("plugin.yaml consumes 须为非空契约列表")
    if not isinstance(produces, list) or not produces or not all(isinstance(x, str) and x for x in produces):
        raise EcosystemValidationError("plugin.yaml produces 须为非空契约列表")
    if kind == "evaluator":
        bad_prod = [c for c in produces if c not in SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS]
        if bad_prod:
            raise EcosystemValidationError(f"evaluator produces 不受支持: {bad_prod!r}")

    params_schema = manifest.get("parameters_schema")
    if not isinstance(params_schema, Mapping):
        raise EcosystemValidationError("plugin.yaml parameters_schema 须为 JSON Schema 对象")
    if params_schema.get("type") != "object" or params_schema.get("additionalProperties") is not False:
        raise EcosystemValidationError(
            "parameters_schema 须声明 type='object' 与 additionalProperties=false"
        )
    try:
        Draft202012Validator.check_schema(dict(params_schema))
    except SchemaError as exc:
        raise EcosystemValidationError(f"parameters_schema 非合法 Draft 2020-12 Schema: {exc}") from exc

    runtime = manifest.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, Mapping):
            raise EcosystemValidationError("plugin.yaml runtime 须为对象")
        req_rel = runtime.get("requirements")
        if req_rel is not None:
            norm_req = _normalize_zip_rel(str(req_rel), is_dir=False)
            if norm_req not in files:
                raise EcosystemValidationError(f"runtime.requirements 指定文件不存在: {req_rel!r}")

    tests = manifest.get("tests") or []
    if not isinstance(tests, list):
        raise EcosystemValidationError("plugin.yaml tests 须为列表")
    for idx, case in enumerate(tests):
        if not isinstance(case, Mapping):
            raise EcosystemValidationError(f"tests[{idx}] 须为对象")
        inp = case.get("input") or case.get("in_dir")
        exp = case.get("expected") or case.get("expected_dir")
        if not isinstance(inp, str) or not isinstance(exp, str):
            raise EcosystemValidationError(f"tests[{idx}] 须声明 input 与 expected")
        norm_in = _normalize_zip_rel(inp.rstrip("/"), is_dir=False)
        norm_exp = _normalize_zip_rel(exp.rstrip("/"), is_dir=False)
        assert norm_in is not None and norm_exp is not None
        in_is_dir = norm_in in dirs or any(f.startswith(f"{norm_in}/") for f in files)
        exp_is_dir = norm_exp in dirs or any(f.startswith(f"{norm_exp}/") for f in files)
        if in_is_dir != exp_is_dir:
            raise EcosystemValidationError(f"tests[{idx}] input 与 expected 必须同为单文件或同为目录")
        if not in_is_dir and (norm_in not in files or norm_exp not in files):
            raise EcosystemValidationError(f"tests[{idx}] 指定的测试样例文件缺失")


def _extract_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    runtime = manifest.get("runtime") or {}
    req_rel = runtime.get("requirements") if isinstance(runtime, Mapping) else None
    norm_req = (
        _normalize_zip_rel(str(req_rel), is_dir=False)
        if isinstance(req_rel, str) and req_rel.strip()
        else None
    )
    return {
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "plugin_id": manifest["plugin_id"],
        "version": manifest["version"],
        "kind": manifest["kind"],
        "name": dict(manifest.get("name") or {}),
        "description": str(manifest.get("description") or ""),
        "license": str(manifest.get("license") or ""),
        "compatibility": dict(manifest.get("compatibility") or {}),
        "consumes": list(manifest["consumes"]),
        "produces": list(manifest["produces"]),
        "entrypoint": str(manifest["entrypoint"]),
        "parameters_schema": dict(manifest["parameters_schema"]),
        "modes": list(
            manifest.get("modes")
            or (["planning"] if manifest["kind"] == "algorithm" else ["calc", "optimization"])
        ),
        "precisions": list(manifest.get("precisions") or ["low", "medium", "high"]),
        "outputs": list(manifest.get("outputs") or ["solution", "metrics"]),
        "solvers": list(manifest.get("solvers") or []),
        "evaluators": list(manifest.get("evaluators") or []),
        "capabilities": list(manifest.get("capabilities") or []),
        "depends_on_solve_result": bool(manifest.get("depends_on_solve_result", False)),
        "accepts_any_target_type": bool(manifest.get("accepts_any_target_type", True)),
        "target_types": list(manifest.get("target_types") or []),
        "needs_history": bool(manifest.get("needs_history", True)),
        "requirements_rel": norm_req,
        "resources": dict(manifest.get("resources") or {}),
        "tests": list(manifest.get("tests") or []),
    }


def _stage_zip_to_build_dir(
    files: Mapping[str, bytes],
    dirs: set[str],
    *,
    build_root: Path,
    plugin_id: str,
    version: str,
) -> tuple[Path, str]:
    build_root.mkdir(parents=True, exist_ok=True)
    rel_name = f"{plugin_id}-{version}-{uuid.uuid4().hex[:8]}"
    dest_dir = (build_root / rel_name).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    for d in sorted(dirs):
        (dest_dir / d).mkdir(parents=True, exist_ok=True)
    for rel, content in sorted(files.items()):
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return dest_dir, rel_name


def _compare_expected_bytes(actual_bytes: bytes, expected_bytes: bytes, rel_path: str) -> None:
    if rel_path.endswith(".json"):
        try:
            actual_obj = json.loads(actual_bytes.decode("utf-8"))
            expected_obj = json.loads(expected_bytes.decode("utf-8"))
        except Exception as exc:
            raise EcosystemValidationError(f"自检输出 JSON 解析失败 ({rel_path}): {exc}") from exc
        if actual_obj != expected_obj:
            raise EcosystemValidationError(
                f"插件断网自检输出与预期不一致 ({rel_path})",
                params={"path": rel_path, "expected": expected_obj, "actual": actual_obj},
            )
        return
    if rel_path.endswith((".yaml", ".yml")):
        actual_obj = _yaml_load(actual_bytes.decode("utf-8"))
        expected_obj = _yaml_load(expected_bytes.decode("utf-8"))
        if actual_obj != expected_obj:
            raise EcosystemValidationError(
                f"插件断网自检 YAML 输出与预期不一致 ({rel_path})",
                params={"path": rel_path},
            )
        return
    if actual_bytes != expected_bytes:
        raise EcosystemValidationError(
            f"插件断网自检产物字节不一致 ({rel_path})",
            params={"path": rel_path},
        )


def _run_selftest_case_via_job_runner(
    *,
    job_runner_client: Any,
    stage_root: Path,
    image: str,
    kind: str,
    plugin_id: str,
    case: Mapping[str, Any],
    files: Mapping[str, bytes],
    dirs: set[str],
    poll_interval: float = 0.05,
    timeout_seconds: float = 120.0,
) -> None:
    case_id = str(case.get("id") or uuid.uuid4().hex[:8])
    exec_id = f"selftest-{uuid.uuid4().hex[:12]}"
    work_dir = stage_root / exec_id
    in_dir = work_dir / "in"
    out_dir = work_dir / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_inp = str(case.get("input") or case.get("in_dir")).rstrip("/")
    raw_exp = str(case.get("expected") or case.get("expected_dir")).rstrip("/")
    norm_in = _normalize_zip_rel(raw_inp, is_dir=False)
    norm_exp = _normalize_zip_rel(raw_exp, is_dir=False)
    assert norm_in is not None and norm_exp is not None
    is_dir_mode = norm_in in dirs or any(f.startswith(f"{norm_in}/") for f in files)

    expected_map: dict[str, bytes] = {}
    if is_dir_mode:
        in_prefix = f"{norm_in}/"
        for fpath, content in files.items():
            if fpath.startswith(in_prefix):
                rel_sub = fpath[len(in_prefix) :]
                dst = in_dir / rel_sub
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(content)
        exp_prefix = f"{norm_exp}/"
        for fpath, content in files.items():
            if fpath.startswith(exp_prefix):
                expected_map[fpath[len(exp_prefix) :]] = content
    else:
        (in_dir / "request.json").write_bytes(files[norm_in])
        default_out = _DEFAULT_SINGLE_OUTPUT_FILES[kind]
        expected_map[default_out] = files[norm_exp]

    spec = StartStageSpec(
        attempt_id=f"selftest-{plugin_id}",
        stage_id=f"test-{case_id}",
        stage_execution_id=exec_id,
        fencing_token=f"fence-{uuid.uuid4().hex[:12]}",
        image=image,
        command=("python", "-m", "pies_sdk.runner"),
        cpu_cores=1.0,
        memory_mb=512,
        pids_limit=32,
        tmpfs_size_mb=64,
    )

    started = False
    try:
        job_runner_client.start_stage(spec)
        started = True
        deadline = time.monotonic() + timeout_seconds
        while True:
            inspection = job_runner_client.inspect_stage(exec_id)
            if not inspection.running and inspection.status in _TERMINAL_STAGE_STATUSES:
                break
            if time.monotonic() >= deadline:
                job_runner_client.stop_stage(exec_id, reason="timed_out", grace_seconds=2)
                raise EcosystemValidationError(
                    f"插件断网自检用例 {case_id!r} 执行超时",
                    params={"case_id": case_id},
                )
            time.sleep(poll_interval)
        if inspection.exit_code != 0:
            raise EcosystemValidationError(
                f"插件断网自检用例 {case_id!r} 退出码非零 ({inspection.exit_code})",
                params={"case_id": case_id, "exit_code": inspection.exit_code},
            )
        for rel_out, exp_bytes in sorted(expected_map.items()):
            candidate_path = out_dir / rel_out
            if not candidate_path.is_file() and not is_dir_mode:
                # 单文件 JSON 简写兜底定位 /work/out 下声明输出文件名或唯一 JSON
                json_candidates = sorted(p for p in out_dir.rglob("*.json") if p.is_file())
                if len(json_candidates) == 1:
                    candidate_path = json_candidates[0]
            if not candidate_path.is_file():
                raise EcosystemValidationError(
                    f"插件断网自检缺少预期输出文件 {rel_out!r} (用例 {case_id!r})",
                    params={"case_id": case_id, "missing_output": rel_out},
                )
            _compare_expected_bytes(candidate_path.read_bytes(), exp_bytes, rel_out)
    finally:
        if started:
            try:
                job_runner_client.cleanup_stage(exec_id, force=True)
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


def _execute_plugin_build_and_selftest(
    *,
    session_factory: Callable[[], Session],
    artifact_id: int,
    zip_bytes: bytes,
    job_runner_client: Any,
    plugin_source_root: Path,
    stage_root: Path,
    pip_index_url: str | None,
) -> LocalArtifactRecord:
    """后台/同步执行镜像构建（--network=none 无依赖，单 --index-url）与断网双模式自检门禁。"""
    manifest, files, dirs = _inspect_plugin_zip(zip_bytes)
    plugin_id = str(manifest["plugin_id"])
    version = str(manifest["version"])
    kind = str(manifest["kind"])
    meta = _extract_metadata_from_manifest(manifest)
    req_rel = meta.get("requirements_rel")

    with session_factory() as db:
        ecosystem_domain.update_artifact(db, artifact_id, install_status="building", diagnostics=None)
        db.commit()

    build_dir: Path | None = None
    built_image: str | None = None
    try:
        build_dir, source_rel = _stage_zip_to_build_dir(
            files, dirs, build_root=plugin_source_root, plugin_id=plugin_id, version=version
        )
        build_result = job_runner_client.build_plugin(
            plugin_id,
            version,
            source_rel,
            requirements_rel=req_rel,
            pip_index_url=pip_index_url,
        )
        built_image = build_result.image
        with session_factory() as db:
            ecosystem_domain.update_artifact(
                db,
                artifact_id,
                install_status="testing",
                image_ref=built_image,
                diagnostics={"build_logs": list(build_result.logs)},
            )
            db.commit()

        for case in meta.get("tests") or ():
            _run_selftest_case_via_job_runner(
                job_runner_client=job_runner_client,
                stage_root=stage_root,
                image=built_image,
                kind=kind,
                plugin_id=plugin_id,
                case=case,
                files=files,
                dirs=dirs,
            )

        with session_factory() as db:
            ready_rec = ecosystem_domain.update_artifact(
                db,
                artifact_id,
                install_status="ready",
                image_ref=built_image,
                diagnostics={"build_logs": list(build_result.logs), "selftest": "passed"},
            )
            db.commit()
            assert ready_rec is not None
            return ready_rec
    except Exception as exc:
        if built_image is not None:
            try:
                job_runner_client.delete_plugin_image(plugin_id, version, force=True)
            except Exception:
                pass
        hint = (
            f"插件 {plugin_id}@{version} 构建或依赖安装失败（{exc}）。请联系管理员安装或配置依赖源"
            if isinstance(exc, JobRunnerClientError)
            else f"插件 {plugin_id}@{version} 断网自检失败（{exc}）"
        )
        with session_factory() as db:
            ecosystem_domain.update_artifact(
                db,
                artifact_id,
                install_status="build_failed",
                image_ref=None,
                diagnostics={"error": hint, "detail": str(exc)},
            )
            db.commit()
        if isinstance(exc, EcosystemValidationError):
            raise
        raise EcosystemValidationError(hint, code="ECO-BUILD-001", params={"detail": str(exc)}) from exc
    finally:
        if build_dir is not None:
            shutil.rmtree(build_dir, ignore_errors=True)


def upload_plugin_zip_case(
    db: Session,
    user: UserRecord,
    *,
    zip_bytes: bytes,
    expected_sha256: str | None = None,
    source_origin: str = "local_upload",
    scheduler: Any | None = None,
    job_runner_client: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
    plugin_source_root: Path | None = None,
    stage_root: Path | None = None,
    wait_for_build: bool = True,
) -> dict[str, Any]:
    """上传算法插件 ZIP：外部核对、归档/清单安全校验、正交审批拦截、受控构建与断网自检。"""
    if source_origin not in SOURCE_ORIGINS:
        raise EcosystemValidationError(f"source_origin 非法: {source_origin!r}")
    raw_bytes = bytes(zip_bytes)
    verify_external_sha256(raw_bytes, expected_sha256)

    manifest, _files, _dirs = _inspect_plugin_zip(raw_bytes)
    kind = str(manifest["kind"])
    stable_id = str(manifest["plugin_id"])
    version = str(manifest["version"])
    meta = _extract_metadata_from_manifest(manifest)

    settings_rec = ecosystem_domain.get_settings(db)
    admin_flag = is_admin_user(db, user.id)
    approval = initial_approval_status(
        settings_rec, is_admin=admin_flag, source_origin=source_origin
    )
    initial_install = "building" if approval == "approved" else "pending"

    try:
        existing = ecosystem_domain.get_artifact_by_identity(db, kind, stable_id, version)
        if existing is not None:
            stored = get_object(db, existing.object_id)
            if stored != raw_bytes:
                raise EcosystemConflictError(
                    "同稳定标识与精确版本的算法插件已存在且归档内容不一致，禁止静默覆盖",
                    params={"kind": kind, "stable_id": stable_id, "version": version},
                )
            install = ecosystem_domain.get_member_install(db, user.id, existing.id)
            if existing.install_status == "ready" and install is None:
                install = ecosystem_domain.upsert_member_install(
                    db, user_id=user.id, artifact=existing, enabled=True
                )
            db.commit()
            payload = serialize_artifact(existing, install=install)
            payload["idempotent"] = True
            return payload

        stored_obj = put_object(
            db,
            raw_bytes,
            PLUGIN_ARCHIVE_MEDIA_TYPE,
            "ecosystem_plugin_archive",
        )
        artifact = ecosystem_domain.create_artifact(
            db,
            kind=kind,
            stable_id=stable_id,
            version=version,
            source_origin=source_origin,
            approval_status=approval,
            install_status=initial_install,
            uploaded_by=user.id,
            approved_by=user.id if approval == "approved" else None,
            approved_at=datetime.now(UTC) if approval == "approved" else None,
            object_id=stored_obj.oid,
            metadata_json=meta,
        )
        attach(
            db,
            stored_obj.id,
            "local_artifact",
            artifact.id,
            ref_entity_type="local_artifacts",
            purpose="生态算法插件压缩包",
        )
        audit_domain.audit(
            db,
            user.id,
            "ecosystem.plugin_uploaded",
            "local_artifact",
            artifact.id,
            actor_type="admin" if admin_flag else "user",
            result={"kind": kind, "stable_id": stable_id, "version": version, "approval_status": approval},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if approval == "pending":
        return serialize_artifact(artifact)

    if job_runner_client is None:
        raise AppError(
            "job_runner 未装配，无法构建算法插件镜像",
            code="ECO-JR-UNAVAILABLE",
        )

    sf = session_factory or _session_factory_from_db(db)
    src_root = plugin_source_root or _default_plugin_source_root()
    stg_root = stage_root or _default_stage_root()

    def _run_job() -> LocalArtifactRecord:
        return _execute_plugin_build_and_selftest(
            session_factory=sf,
            artifact_id=artifact.id,
            zip_bytes=raw_bytes,
            job_runner_client=job_runner_client,
            plugin_source_root=src_root,
            stage_root=stg_root,
            pip_index_url=settings_rec.pip_index_url,
        )

    if scheduler is not None:
        fut = scheduler.submit_plugin_build(_run_job)
        if not wait_for_build:
            return serialize_artifact(artifact)
        built = fut.result()
    else:
        built = _run_job()

    db.expire_all()
    install = None
    if built.install_status == "ready":
        install = ecosystem_domain.upsert_member_install(
            db, user_id=user.id, artifact=built, enabled=True
        )
        db.commit()
    payload = serialize_artifact(built, install=install)
    payload["idempotent"] = False
    return payload


def _session_factory_from_db(db: Session) -> Callable[[], Session]:
    from sqlalchemy.orm import sessionmaker

    bind = db.get_bind()
    factory = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
    return factory


def approve_artifact_case(
    db: Session,
    admin_user: UserRecord,
    artifact_id: int,
    *,
    scheduler: Any | None = None,
    job_runner_client: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
    plugin_source_root: Path | None = None,
    stage_root: Path | None = None,
    wait_for_build: bool = True,
) -> dict[str, Any]:
    """管理员审批通过待审生态制品；设备模型直接 ready，插件触发受控构建与断网自检。"""
    require_admin(db, admin_user.id)
    artifact = ecosystem_domain.get_artifact(db, artifact_id, for_update=True)
    if artifact is None:
        raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
    if artifact.approval_status == "rejected":
        raise EcosystemConflictError("已拒绝制品不可直接审批通过", params={"artifact_id": artifact_id})
    if artifact.approval_status == "approved" and artifact.install_status == "ready":
        return serialize_artifact(artifact)

    settings_rec = ecosystem_domain.get_settings(db)
    next_install = "ready" if artifact.kind == "device_model" else "building"
    try:
        updated = ecosystem_domain.update_artifact(
            db,
            artifact_id,
            approval_status="approved",
            install_status=next_install,
            approved_by=admin_user.id,
            approved_at=datetime.now(UTC),
        )
        assert updated is not None
        audit_domain.audit(
            db,
            admin_user.id,
            "ecosystem.artifact_approved",
            "local_artifact",
            artifact_id,
            actor_type="admin",
            result={"kind": updated.kind, "stable_id": updated.stable_id, "version": updated.version},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if updated.kind == "device_model":
        return serialize_artifact(updated)

    if job_runner_client is None:
        raise AppError("job_runner 未装配，无法构建算法插件镜像", code="ECO-JR-UNAVAILABLE")

    raw_bytes = get_object(db, updated.object_id)
    sf = session_factory or _session_factory_from_db(db)
    src_root = plugin_source_root or _default_plugin_source_root()
    stg_root = stage_root or _default_stage_root()

    def _run_job() -> LocalArtifactRecord:
        return _execute_plugin_build_and_selftest(
            session_factory=sf,
            artifact_id=updated.id,
            zip_bytes=raw_bytes,
            job_runner_client=job_runner_client,
            plugin_source_root=src_root,
            stage_root=stg_root,
            pip_index_url=settings_rec.pip_index_url,
        )

    if scheduler is not None:
        fut = scheduler.submit_plugin_build(_run_job)
        if not wait_for_build:
            return serialize_artifact(updated)
        built = fut.result()
    else:
        built = _run_job()

    db.expire_all()
    return serialize_artifact(built)


def reject_artifact_case(
    db: Session,
    admin_user: UserRecord,
    artifact_id: int,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """管理员拒绝待审生态制品。"""
    require_admin(db, admin_user.id)
    artifact = ecosystem_domain.get_artifact(db, artifact_id, for_update=True)
    if artifact is None:
        raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
    try:
        updated = ecosystem_domain.update_artifact(
            db,
            artifact_id,
            approval_status="rejected",
            install_status="disabled",
            approved_by=admin_user.id,
            approved_at=datetime.now(UTC),
            diagnostics={"rejected_reason": reason} if reason else None,
        )
        assert updated is not None
        audit_domain.audit(
            db,
            admin_user.id,
            "ecosystem.artifact_rejected",
            "local_artifact",
            artifact_id,
            actor_type="admin",
            result={"reason": reason},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return serialize_artifact(updated)


def uninstall_artifact_image_case(
    db: Session,
    admin_user: UserRecord,
    artifact_id: int,
    *,
    job_runner_client: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """两阶段无状态并发安全卸载：FOR UPDATE 下架防新引用 + 核对非终态任务 + delete_plugin_image。"""
    require_admin(db, admin_user.id)
    try:
        artifact = ecosystem_domain.get_artifact(db, artifact_id, for_update=True)
        if artifact is None:
            raise EcosystemArtifactNotFoundError(
                "生态制品不存在", params={"artifact_id": artifact_id}
            )
        disabled = ecosystem_domain.update_artifact(db, artifact_id, install_status="disabled")
        assert disabled is not None
        inflight = tasks_domain.count_inflight_artifact_refs(
            db,
            kind=disabled.kind,
            stable_id=disabled.stable_id,
            version=disabled.version,
        )
        if inflight > 0:
            audit_domain.audit(
                db,
                admin_user.id,
                "ecosystem.artifact_disabled_inflight",
                "local_artifact",
                artifact_id,
                actor_type="admin",
                result={"inflight_count": inflight},
            )
            db.commit()
            raise EcosystemUninstallInUseError(
                f"已下架阻止新任务，当前仍有 {inflight} 个在途任务使用中，请待任务结束后再执行镜像清理",
                params={
                    "artifact_id": artifact_id,
                    "kind": disabled.kind,
                    "stable_id": disabled.stable_id,
                    "version": disabled.version,
                    "inflight_count": inflight,
                },
            )
        db.commit()
    except EcosystemUninstallInUseError:
        raise
    except Exception:
        db.rollback()
        raise

    image_deleted = False
    image_status = "not_applicable"
    if disabled.kind != "device_model" and disabled.image_ref:
        if job_runner_client is None:
            raise AppError("job_runner 未装配，无法清理插件镜像", code="ECO-JR-UNAVAILABLE")
        try:
            del_res = job_runner_client.delete_plugin_image(
                disabled.stable_id, disabled.version, force=bool(force)
            )
        except JobRunnerConflictError as exc:
            raise EcosystemUninstallInUseError(
                "镜像正被运行容器使用，请待容器退出后再执行镜像清理",
                params={"artifact_id": artifact_id, "detail": str(exc)},
            ) from exc
        image_deleted = bool(del_res.deleted)
        image_status = str(del_res.status)

    try:
        final_rec = ecosystem_domain.update_artifact(db, artifact_id, image_ref=None)
        assert final_rec is not None
        audit_domain.audit(
            db,
            admin_user.id,
            "ecosystem.artifact_uninstalled",
            "local_artifact",
            artifact_id,
            actor_type="admin",
            result={"image_deleted": image_deleted, "image_status": image_status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "artifact": serialize_artifact(final_rec),
        "image_deleted": image_deleted,
        "image_status": image_status,
    }


def list_catalog_artifacts_case(
    db: Session,
    user: UserRecord,
    *,
    kind: str | None = None,
    approval_status: str | None = None,
) -> dict[str, Any]:
    """列出本地生态目录条目，并附带当前成员的安装与启用态。"""
    if kind is not None and kind not in ("device_model",) + PLUGIN_KINDS:
        raise EcosystemValidationError(f"不支持的制品 kind: {kind!r}")
    artifacts = ecosystem_domain.list_artifacts(
        db, kind=kind, approval_status=approval_status
    )
    installs = {
        rec.artifact_id: rec
        for rec in ecosystem_domain.list_member_installs(db, user.id, kind=kind)
    }
    return {
        "items": [
            serialize_artifact(item, install=installs.get(item.id))
            for item in artifacts
        ]
    }


def install_artifact_for_user_case(
    db: Session,
    user: UserRecord,
    artifact_id: int,
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    """当前成员安装/启用指定精确版本（同 ID 多版本可并存）。"""
    artifact = ecosystem_domain.get_artifact(db, artifact_id)
    if artifact is None:
        raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
    if artifact.approval_status != "approved" or artifact.install_status != "ready":
        raise EcosystemConflictError(
            "仅已审批且状态为 ready 的制品允许成员安装启用",
            params={
                "artifact_id": artifact_id,
                "approval_status": artifact.approval_status,
                "install_status": artifact.install_status,
            },
        )
    try:
        install = ecosystem_domain.upsert_member_install(
            db, user_id=user.id, artifact=artifact, enabled=enabled
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return serialize_artifact(artifact, install=install)


def set_member_artifact_enabled_case(
    db: Session,
    user: UserRecord,
    artifact_id: int,
    *,
    enabled: bool,
) -> dict[str, Any]:
    """当前成员停用/重启用已安装的精确版本。"""
    artifact = ecosystem_domain.get_artifact(db, artifact_id)
    if artifact is None:
        raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
    try:
        install = ecosystem_domain.set_member_install_enabled(
            db, user.id, artifact_id, enabled=enabled
        )
        if install is None:
            raise EcosystemArtifactNotFoundError(
                "当前成员尚未安装该制品版本", params={"artifact_id": artifact_id}
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return serialize_artifact(artifact, install=install)


def uninstall_member_artifact_case(
    db: Session,
    user: UserRecord,
    artifact_id: int,
) -> dict[str, Any]:
    """成员卸载自身安装记录（不删除共享目录记录与容器镜像）。"""
    try:
        removed = ecosystem_domain.delete_member_install(db, user.id, artifact_id)
        if not removed:
            raise EcosystemArtifactNotFoundError(
                "当前成员尚未安装该制品版本", params={"artifact_id": artifact_id}
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"artifact_id": artifact_id, "uninstalled": True}


# Unused _yaml_dump import check prevention: kept for plugin metadata serialization if needed.
_ = _yaml_dump

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
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
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
    validate_ingest_source_origin,
    verify_external_sha256,
)
from iesplan.computation.contracts.common import check_finite_json, validate_parameters_schema
from iesplan.computation.contracts.compatibility import (
    ENGINEERING_SOLUTION_CONTRACT,
    FINANCIAL_EVALUATION_CONTRACT,
    PREDICTION_OUTPUT_CONTRACT,
    PRIMAL_SOLUTION_CONTRACT,
    STAGE_INPUT_CONTRACT,
    SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS,
    StagePluginSpec,
)
from iesplan.computation.runtime.job_runner_client import (
    JobRunnerClientError,
    JobRunnerConflictError,
    StartStageSpec,
)
from iesplan.config import settings as app_settings
from iesplan.core.errors import AppError
from iesplan.core.yamlmini import load as _yaml_load
from iesplan.ecosystem import (
    PLUGIN_KINDS,
    EcosystemArtifactNotFoundError,
    EcosystemConflictError,
    EcosystemUninstallInUseError,
    EcosystemValidationError,
    LocalArtifactRecord,
)
from iesplan.identity.contracts import UserRecord
from iesplan.storage import attach, detach, find_refs_by_owner, get_object, put_object

PLUGIN_ARCHIVE_MEDIA_TYPE = "application/zip"
_MAX_ZIP_BYTES = 50 * 1024 * 1024
_MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
_MAX_ZIP_ENTRIES = 1000
_FORBIDDEN_FILES = frozenset({"Dockerfile", ".dockerignore"})
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CLAUSE_RE = re.compile(r"^(>=|<=|>|<|==|!=)?\s*(\d+\.\d+\.\d+)$")
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z][A-Za-z0-9_]*$"
)
_TERMINAL_STAGE_STATUSES = frozenset({"exited", "dead"})
_PLUGIN_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "computation" / "schema" / "plugin-1.0.0.schema.json"
)
CURRENT_PIES_CONTRACT_VERSION = "1.2.0"
CURRENT_SDK_CONTRACT_VERSION = "1.2.0"

_DEFAULT_SINGLE_OUTPUT_FILES: dict[str, str] = {
    "predictor": "prediction.json",
    "solver": "primal_solution.json",
    "algorithm": "solution.json",
}


@lru_cache(maxsize=1)
def _load_plugin_schema() -> dict[str, Any]:
    return json.loads(_PLUGIN_SCHEMA_PATH.read_text(encoding="utf-8"))


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


def _extract_zip_entries_unchecked(zip_bytes: bytes) -> tuple[dict[str, bytes], set[str]]:
    """从已通过入口校验的归档读取文件与目录集合，供延迟构建路径复原源码树（不重复规则校验）。"""
    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
        for info in zf.infolist():
            is_dir = info.is_dir() or info.filename.endswith("/")
            raw = info.filename.rstrip("/") if is_dir else info.filename
            if raw in ("", "."):
                continue
            rel = "/".join(p for p in raw.split("/") if p != ".")
            if is_dir:
                dirs.add(rel)
            else:
                files[rel] = zf.read(info.filename)
    return files, dirs


def _inspect_plugin_zip(zip_bytes: bytes) -> tuple[dict[str, Any], dict[str, bytes], set[str]]:
    """入口唯一安全解压检查与完整 ies.algorithm-plugin@1.0.0 清单校验。"""
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
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise EcosystemValidationError(f"插件 ZIP 禁止包含符号链接: {info.filename!r}")
            file_fmt = stat.S_IFMT(unix_mode)
            if file_fmt != 0 and not (info.is_dir() or stat.S_ISREG(unix_mode)):
                raise EcosystemValidationError(f"插件 ZIP 禁止包含非常规文件条目: {info.filename!r}")
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


def _parse_semver(version: str) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(version.strip()) if isinstance(version, str) else None
    if not match:
        raise EcosystemValidationError(f"非法语义化版本: {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _check_version_constraint(version: str, constraint: str, *, target_name: str) -> None:
    actual = _parse_semver(version)
    if not isinstance(constraint, str) or not constraint.strip():
        raise EcosystemValidationError(f"compatibility.{target_name} 约束不能为空")
    for clause in (part.strip() for part in constraint.split(",")):
        if not clause:
            raise EcosystemValidationError(f"compatibility.{target_name} 存在空子句: {constraint!r}")
        match = _CLAUSE_RE.fullmatch(clause)
        if not match:
            raise EcosystemValidationError(
                f"compatibility.{target_name} 语法非法: {clause!r}",
                params={"target": target_name, "clause": clause},
            )
        op = match.group(1) or "=="
        expected = _parse_semver(match.group(2))
        ok = (
            (op == "==" and actual == expected)
            or (op == "!=" and actual != expected)
            or (op == ">=" and actual >= expected)
            or (op == "<=" and actual <= expected)
            or (op == ">" and actual > expected)
            or (op == "<" and actual < expected)
        )
        if not ok:
            raise EcosystemValidationError(
                f"插件与当前 {target_name} 版本 {version!r} 不兼容: 要求 {constraint!r}",
                code="ECO-COMPAT-001",
                params={"target": target_name, "actual": version, "required": constraint},
            )


def _check_nested_object_schemas(node: Mapping[str, Any], path: str) -> None:
    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise EcosystemValidationError(
            f"{path} 必须显式声明 additionalProperties: false",
            params={"path": path},
        )
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, Mapping):
                _check_nested_object_schemas(prop_schema, f"{path}.properties.{prop_name}")
    for key in ("$defs", "definitions"):
        defs = node.get(key)
        if isinstance(defs, Mapping):
            for def_name, def_schema in defs.items():
                if isinstance(def_schema, Mapping):
                    _check_nested_object_schemas(def_schema, f"{path}.{key}.{def_name}")
    items = node.get("items")
    if isinstance(items, Mapping):
        _check_nested_object_schemas(items, f"{path}.items")
    for comb in ("allOf", "anyOf", "oneOf"):
        branches = node.get(comb)
        if isinstance(branches, list):
            for idx, branch in enumerate(branches):
                if isinstance(branch, Mapping):
                    _check_nested_object_schemas(branch, f"{path}.{comb}[{idx}]")


def _validate_manifest_dict(
    manifest: Mapping[str, Any],
    *,
    files: Mapping[str, bytes],
    dirs: set[str],
) -> None:
    """严格校验完整 ies.algorithm-plugin@1.0.0 契约与兼容性（不 import 用户代码）。"""
    try:
        check_finite_json(dict(manifest), "plugin.yaml")
    except (TypeError, ValueError) as exc:
        raise EcosystemValidationError(f"plugin.yaml 含非有限或非法数值: {exc}") from exc

    if manifest.get("schema") != "ies.algorithm-plugin" or manifest.get("schema_version") != "1.0.0":
        raise EcosystemValidationError(
            "plugin.yaml 须声明 schema: ies.algorithm-plugin 与 schema_version: '1.0.0'",
            code="ECO-COMPAT-001",
        )

    schema_validator = Draft202012Validator(_load_plugin_schema())
    errors = sorted(schema_validator.iter_errors(dict(manifest)), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise EcosystemValidationError(
            f"plugin.yaml 不符合 ies.algorithm-plugin@1.0.0 契约 [{loc}]: {first.message}",
            params={"location": loc, "detail": first.message},
        )

    kind = str(manifest["kind"])
    if kind not in PLUGIN_KINDS:
        raise EcosystemValidationError(f"plugin.yaml kind 非法: {kind!r}")
    plugin_id = str(manifest["plugin_id"])
    validate_artifact_stable_id(plugin_id, expected_kind=kind)

    compat = manifest["compatibility"]
    if "pies" in compat:
        _check_version_constraint(
            CURRENT_PIES_CONTRACT_VERSION, str(compat["pies"]), target_name="pies"
        )
    if "sdk" in compat:
        _check_version_constraint(
            CURRENT_SDK_CONTRACT_VERSION, str(compat["sdk"]), target_name="sdk"
        )

    entrypoint = str(manifest["entrypoint"])
    if not _ENTRYPOINT_RE.fullmatch(entrypoint):
        raise EcosystemValidationError(f"plugin.yaml entrypoint 格式非法: {entrypoint!r}")
    mod_part, func_part = entrypoint.split(":", 1)
    if func_part.startswith("_"):
        raise EcosystemValidationError(f"entrypoint 函数不可为私有符号: {entrypoint!r}")
    mod_rel = mod_part.replace(".", "/")
    if f"{mod_rel}.py" not in files and f"{mod_rel}/__init__.py" not in files:
        raise EcosystemValidationError(
            f"entrypoint 指向的模块文件不存在于插件包内: {mod_part!r}"
        )

    consumes = manifest.get("consumes")
    produces = manifest.get("produces")
    if not isinstance(consumes, list) or not consumes:
        raise EcosystemValidationError("plugin.yaml consumes 须为非空契约列表")
    if not isinstance(produces, list) or not produces:
        raise EcosystemValidationError("plugin.yaml produces 须为非空契约列表")
    if kind == "predictor":
        if STAGE_INPUT_CONTRACT not in consumes or PREDICTION_OUTPUT_CONTRACT not in produces:
            raise EcosystemValidationError(
                f"predictor 须声明 consumes=[{STAGE_INPUT_CONTRACT!r}] "
                f"与 produces=[{PREDICTION_OUTPUT_CONTRACT!r}]"
            )
    elif kind == "generator":
        if STAGE_INPUT_CONTRACT not in consumes:
            raise EcosystemValidationError(f"generator 须消费 {STAGE_INPUT_CONTRACT!r}")
    elif kind == "solver":
        if PRIMAL_SOLUTION_CONTRACT not in produces:
            raise EcosystemValidationError(f"solver 须产出 {PRIMAL_SOLUTION_CONTRACT!r}")
    elif kind == "evaluator":
        bad_prod = [c for c in produces if c not in SUPPORTED_EVALUATOR_OUTPUT_CONTRACTS]
        if bad_prod or ENGINEERING_SOLUTION_CONTRACT not in consumes:
            raise EcosystemValidationError(
                f"evaluator 契约非法（consumes={consumes!r}, produces={produces!r}）"
            )
    elif kind == "algorithm":
        if STAGE_INPUT_CONTRACT not in consumes or ENGINEERING_SOLUTION_CONTRACT not in produces:
            raise EcosystemValidationError(
                f"algorithm 须声明消费 {STAGE_INPUT_CONTRACT!r} 并产出 {ENGINEERING_SOLUTION_CONTRACT!r}"
            )

    params_schema = manifest["parameters_schema"]
    try:
        validate_parameters_schema(params_schema, "parameters_schema")
    except (TypeError, ValueError) as exc:
        raise EcosystemValidationError(f"parameters_schema 非法: {exc}") from exc
    _check_nested_object_schemas(params_schema, "parameters_schema")

    try:
        StagePluginSpec.from_manifest(manifest)
    except (TypeError, ValueError) as exc:
        raise EcosystemValidationError(f"插件阶段能力契约投影失败: {exc}") from exc

    resources = manifest["resources"]
    default_res = resources["default"]
    max_res = resources["maximum"]
    for key in ("cpu", "memory_mb", "timeout_seconds", "process_limit"):
        dval = default_res[key]
        mval = max_res[key]
        if isinstance(dval, bool) or isinstance(mval, bool):
            raise EcosystemValidationError(f"resources.{key} 禁止布尔值")
        if dval > mval:
            raise EcosystemValidationError(
                f"resources.default.{key} ({dval}) 不得大于 resources.maximum.{key} ({mval})"
            )

    iteration = manifest.get("iteration")
    if isinstance(iteration, Mapping) and iteration.get("enabled"):
        if "max_iterations_default" not in iteration or "convergence_metric" not in iteration:
            raise EcosystemValidationError(
                "iteration.enabled=true 时必须声明 max_iterations_default 与 convergence_metric"
            )

    runtime = manifest["runtime"]
    req_rel = runtime.get("requirements")
    if req_rel is not None:
        norm_req = _normalize_zip_rel(str(req_rel), is_dir=False)
        if norm_req not in files:
            raise EcosystemValidationError(f"runtime.requirements 指定文件不存在: {req_rel!r}")
        try:
            files[norm_req].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EcosystemValidationError(f"runtime.requirements 非合法 UTF-8 文本: {exc}") from exc

    tests = manifest.get("tests") or ()
    seen_test_ids: set[str] = set()
    for idx, case in enumerate(tests):
        case_id = str(case["id"])
        if case_id in seen_test_ids:
            raise EcosystemValidationError(f"tests[{idx}] 存在重复用例 id: {case_id!r}")
        seen_test_ids.add(case_id)
        if "in" in case:
            norm_in = _normalize_zip_rel(str(case["in"]).rstrip("/"), is_dir=False)
            norm_exp = _normalize_zip_rel(str(case["expected"]).rstrip("/"), is_dir=False)
            assert norm_in is not None and norm_exp is not None
            in_has_dir = norm_in in dirs or any(f.startswith(f"{norm_in}/") for f in files)
            exp_files = [f for f in files if f.startswith(f"{norm_exp}/")]
            if not in_has_dir or not exp_files:
                raise EcosystemValidationError(
                    f"tests[{idx}] 目录测试样例缺失或 expected 目录为空"
                )
        else:
            norm_in = _normalize_zip_rel(str(case["input"]), is_dir=False)
            norm_exp = _normalize_zip_rel(str(case["expected"]), is_dir=False)
            assert norm_in is not None and norm_exp is not None
            if norm_in not in files or norm_exp not in files:
                raise EcosystemValidationError(f"tests[{idx}] 单文件测试样例缺失")


def _extract_metadata_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    runtime = dict(manifest["runtime"])
    req_rel = runtime.get("requirements")
    norm_req = (
        _normalize_zip_rel(str(req_rel), is_dir=False)
        if isinstance(req_rel, str) and req_rel.strip()
        else None
    )
    meta: dict[str, Any] = {
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "plugin_id": manifest["plugin_id"],
        "version": manifest["version"],
        "kind": manifest["kind"],
        "name": dict(manifest["name"]),
        "description": str(manifest["description"]),
        "compatibility": dict(manifest["compatibility"]),
        "consumes": list(manifest["consumes"]),
        "produces": list(manifest["produces"]),
        "runtime": runtime,
        "entrypoint": str(manifest["entrypoint"]),
        "parameters_schema": dict(manifest["parameters_schema"]),
        "resources": dict(manifest["resources"]),
        "requirements_rel": norm_req,
        "tests": list(manifest.get("tests") or []),
        "advanced": dict(manifest.get("advanced") or {}),
        "extensions": dict(manifest.get("extensions") or {}),
    }
    if manifest.get("license") is not None:
        meta["license"] = str(manifest["license"])
    if manifest.get("iteration") is not None:
        meta["iteration"] = dict(manifest["iteration"])
    return meta


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


def _resolve_single_output_filename(kind: str, produces: list[str] | tuple[str, ...]) -> str:
    if kind == "evaluator":
        if FINANCIAL_EVALUATION_CONTRACT in produces:
            return "financial_evaluation.json"
        return "evaluation_report.json"
    if kind in _DEFAULT_SINGLE_OUTPUT_FILES:
        return _DEFAULT_SINGLE_OUTPUT_FILES[kind]
    return "bundle/bundle.yaml"


def _run_selftest_case_via_job_runner(
    *,
    job_runner_client: Any,
    stage_root: Path,
    image: str,
    kind: str,
    produces: list[str] | tuple[str, ...],
    plugin_id: str,
    case: Mapping[str, Any],
    files: Mapping[str, bytes],
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

    is_dir_mode = "in" in case
    raw_inp = str(case["in"] if is_dir_mode else case["input"]).rstrip("/")
    raw_exp = str(case["expected"]).rstrip("/")
    norm_in = "/".join(p for p in raw_inp.split("/") if p != ".")
    norm_exp = "/".join(p for p in raw_exp.split("/") if p != ".")

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
        expected_map[_resolve_single_output_filename(kind, produces)] = files[norm_exp]

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
    primary_exc: BaseException | None = None
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
                json_candidates = sorted(p for p in out_dir.rglob("*.json") if p.is_file())
                if len(json_candidates) == 1:
                    candidate_path = json_candidates[0]
            if not candidate_path.is_file():
                raise EcosystemValidationError(
                    f"插件断网自检缺少预期输出文件 {rel_out!r} (用例 {case_id!r})",
                    params={"case_id": case_id, "missing_output": rel_out},
                )
            _compare_expected_bytes(candidate_path.read_bytes(), exp_bytes, rel_out)
    except BaseException as exc:
        primary_exc = exc
        raise
    finally:
        cleanup_errors: list[str] = []
        if started:
            try:
                job_runner_client.cleanup_stage(exec_id, force=True)
            except Exception as exc:
                cleanup_errors.append(f"cleanup_stage: {exc}")
        if work_dir.exists():
            try:
                shutil.rmtree(work_dir)
            except Exception as exc:
                cleanup_errors.append(f"rmtree({work_dir}): {exc}")
        if cleanup_errors:
            joined = "; ".join(cleanup_errors)
            if primary_exc is None:
                raise EcosystemValidationError(
                    f"插件断网自检清理阶段失败 ({case_id!r}): {joined}",
                    code="ECO-SELFTEST-CLEANUP",
                    params={"case_id": case_id, "cleanup_error": joined},
                )
            raise EcosystemValidationError(
                f"{primary_exc}（且清理阶段失败: {joined}）",
                code="ECO-SELFTEST-CLEANUP",
                params={"case_id": case_id, "cleanup_error": joined},
            ) from primary_exc


def _attempt_delete_built_image(
    job_runner_client: Any, *, plugin_id: str, version: str
) -> str | None:
    """尝试删除构建镜像；成功返回 None，失败返回错误信息字符串（不吞异常）。"""
    try:
        del_res = job_runner_client.delete_plugin_image(plugin_id, version, force=True)
        if not (del_res.deleted or del_res.status == "not_found"):
            return f"delete_plugin_image 未完成清理 (status={del_res.status!r})"
        return None
    except Exception as exc:
        return str(exc)


def _execute_plugin_build_and_selftest(
    *,
    session_factory: Callable[[], Session],
    artifact_id: int,
    zip_bytes: bytes | None,
    job_runner_client: Any,
    plugin_source_root: Path,
    stage_root: Path,
    pip_index_url: str | None,
    preextracted: tuple[dict[str, bytes], set[str]] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> LocalArtifactRecord:
    """推进构建与断网自检门禁：清理失败保留 image_ref 与构建目录异常传播。"""
    with session_factory() as db:
        rec = ecosystem_domain.get_artifact(db, artifact_id)
        if rec is None:
            raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
        if rec.approval_status != "approved" or rec.install_status != "building":
            raise EcosystemConflictError(
                "制品当前状态不允许继续构建（可能已下架或变更状态）",
                params={
                    "artifact_id": artifact_id,
                    "approval_status": rec.approval_status,
                    "install_status": rec.install_status,
                },
            )
        meta = dict(metadata or rec.metadata_json)
        plugin_id = rec.stable_id
        version = rec.version
        kind = rec.kind

    if preextracted is not None:
        files, dirs = preextracted
    else:
        assert zip_bytes is not None
        files, dirs = _extract_zip_entries_unchecked(zip_bytes)

    req_rel = meta.get("requirements_rel")
    produces = list(meta.get("produces") or ())
    build_dir: Path | None = None
    built_image: str | None = None
    primary_exc: BaseException | None = None

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
            current = ecosystem_domain.get_artifact(db, artifact_id)
            if (
                current is None
                or current.approval_status != "approved"
                or current.install_status != "building"
                or current.version != version
            ):
                del_err = _attempt_delete_built_image(
                    job_runner_client, plugin_id=plugin_id, version=version
                )
                if current is not None and del_err is not None:
                    ecosystem_domain.update_artifact(
                        db,
                        artifact_id,
                        image_ref=built_image,
                        diagnostics={"cleanup_error": del_err, "cleanup_retryable": True},
                    )
                    db.commit()
                raise EcosystemConflictError(
                    "制品在构建期间已被下架或改变状态，已中止发布"
                    + (f"（镜像清理失败: {del_err}）" if del_err else ""),
                    params={"artifact_id": artifact_id, "cleanup_error": del_err},
                )
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
                produces=produces,
                plugin_id=plugin_id,
                case=case,
                files=files,
            )

        with session_factory() as db:
            current = ecosystem_domain.get_artifact(db, artifact_id)
            if (
                current is None
                or current.approval_status != "approved"
                or current.install_status != "testing"
                or current.version != version
            ):
                del_err = _attempt_delete_built_image(
                    job_runner_client, plugin_id=plugin_id, version=version
                )
                if current is not None:
                    ecosystem_domain.update_artifact(
                        db,
                        artifact_id,
                        image_ref=built_image if del_err is not None else None,
                        diagnostics=(
                            {"cleanup_error": del_err, "cleanup_retryable": True}
                            if del_err is not None
                            else {"stale_build_aborted": True}
                        ),
                    )
                    db.commit()
                raise EcosystemConflictError(
                    "制品在自检期间已被下架或改变状态，禁止回流为 ready"
                    + (f"（镜像清理失败: {del_err}）" if del_err else ""),
                    params={"artifact_id": artifact_id, "cleanup_error": del_err},
                )
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
    except EcosystemConflictError as exc:
        primary_exc = exc
        raise
    except Exception as exc:
        del_err: str | None = None
        if built_image is not None:
            del_err = _attempt_delete_built_image(
                job_runner_client, plugin_id=plugin_id, version=version
            )
        image_ref_to_keep = built_image if del_err is not None else None
        hint = (
            f"插件 {plugin_id}@{version} 构建或依赖安装失败（{exc}）。请联系管理员安装或配置依赖源"
            if isinstance(exc, JobRunnerClientError)
            else f"插件 {plugin_id}@{version} 断网自检失败（{exc}）"
        )
        diag: dict[str, Any] = {"error": hint, "detail": str(exc)}
        if del_err is not None:
            diag["cleanup_error"] = del_err
            diag["cleanup_retryable"] = True
        with session_factory() as db:
            cur = ecosystem_domain.get_artifact(db, artifact_id)
            if cur is not None:
                next_status = "disabled" if cur.install_status == "disabled" else "build_failed"
                ecosystem_domain.update_artifact(
                    db,
                    artifact_id,
                    install_status=next_status,
                    image_ref=image_ref_to_keep,
                    diagnostics=diag,
                )
                db.commit()
        if del_err is not None:
            err = AppError(
                f"{hint}；且构建镜像删除失败，已保留 image_ref 以便重试清理: {del_err}",
                code="ECO-IMAGE-CLEANUP-FAILED",
                params={"detail": str(exc), "cleanup_error": del_err, "image_ref": built_image},
            )
            primary_exc = err
            raise err from exc
        if isinstance(exc, EcosystemValidationError):
            primary_exc = exc
            raise
        err2 = EcosystemValidationError(
            hint, code="ECO-BUILD-001", params={"detail": str(exc)}
        )
        primary_exc = err2
        raise err2 from exc
    finally:
        cleanup_dir_error: str | None = None
        if build_dir is not None and build_dir.exists():
            try:
                shutil.rmtree(build_dir)
            except Exception as exc:
                cleanup_dir_error = str(exc)
        if cleanup_dir_error is not None:
            if primary_exc is None:
                raise EcosystemValidationError(
                    f"插件构建临时目录清理失败: {cleanup_dir_error}",
                    code="ECO-BUILD-CLEANUP",
                )
            raise EcosystemValidationError(
                f"{primary_exc}（且构建临时目录清理失败: {cleanup_dir_error}）",
                code="ECO-BUILD-CLEANUP",
                params={"cleanup_error": cleanup_dir_error},
            ) from primary_exc


def upload_plugin_zip_case(
    db: Session,
    user: UserRecord,
    *,
    zip_bytes: bytes,
    expected_sha256: str | None = None,
    source_origin: str = "local_upload",
    trusted_market: bool = False,
    scheduler: Any | None = None,
    job_runner_client: Any | None = None,
    session_factory: Callable[[], Session] | None = None,
    plugin_source_root: Path | None = None,
    stage_root: Path | None = None,
    wait_for_build: bool = True,
) -> dict[str, Any]:
    """上传算法插件 ZIP：外部核对、归档/清单全量校验、正交审批拦截、受控构建与断网自检。"""
    validate_ingest_source_origin(source_origin, trusted_market=trusted_market)
    raw_bytes = bytes(zip_bytes)
    verify_external_sha256(raw_bytes, expected_sha256)

    manifest, files, dirs = _inspect_plugin_zip(raw_bytes)
    kind = str(manifest["kind"])
    stable_id = str(manifest["plugin_id"])
    version = str(manifest["version"])
    meta = _extract_metadata_from_manifest(manifest)

    settings_rec = ecosystem_domain.get_settings(db)
    admin_flag = is_admin_user(db, user.id)
    approval = initial_approval_status(
        settings_rec,
        is_admin=admin_flag,
        source_origin=source_origin,
        trusted_market=trusted_market,
    )
    initial_install = "building" if approval == "approved" else "pending"

    try:
        existing = ecosystem_domain.get_artifact_by_stable_id(db, kind, stable_id)
        if existing is not None:
            if existing.version == version:
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
                payload["duplicate"] = True
                return payload

            # 不同版本覆盖升级：先核对旧版本无在途 CalcSnapshot 引用，再清理旧镜像并原地替换
            prev_version = existing.version
            inflight = tasks_domain.count_inflight_artifact_refs(
                db, kind=kind, stable_id=stable_id, version=prev_version
            )
            if inflight > 0:
                raise EcosystemUninstallInUseError(
                    f"旧版本 {prev_version!r} 仍有 {inflight} 个在途任务使用中，禁止覆盖升级",
                    params={
                        "artifact_id": existing.id,
                        "kind": kind,
                        "stable_id": stable_id,
                        "version": prev_version,
                        "inflight_count": inflight,
                    },
                )
            if existing.image_ref:
                if job_runner_client is None:
                    raise AppError(
                        "job_runner 未装配，无法清理旧版本插件镜像",
                        code="ECO-JR-UNAVAILABLE",
                    )
                del_err = _attempt_delete_built_image(
                    job_runner_client, plugin_id=stable_id, version=prev_version
                )
                if del_err is not None:
                    raise AppError(
                        f"旧版本插件镜像清理失败，已保留 image_ref 以便重试清理: {del_err}",
                        code="ECO-IMAGE-CLEANUP-FAILED",
                        params={
                            "artifact_id": existing.id,
                            "image_ref": existing.image_ref,
                            "cleanup_error": del_err,
                        },
                    )

            stored_obj = put_object(
                db,
                raw_bytes,
                PLUGIN_ARCHIVE_MEDIA_TYPE,
                "ecosystem_plugin_archive",
            )
            for ref in find_refs_by_owner(db, "local_artifact", existing.id, "local_artifacts"):
                detach(
                    db,
                    ref["object_id"],
                    "local_artifact",
                    existing.id,
                    ref_entity_type="local_artifacts",
                )
            artifact = ecosystem_domain.update_artifact(
                db,
                existing.id,
                version=version,
                source_origin=source_origin,
                uploaded_by=user.id,
                object_id=stored_obj.oid,
                approval_status=approval,
                install_status=initial_install,
                approved_by=user.id if approval == "approved" else None,
                approved_at=datetime.now(UTC) if approval == "approved" else None,
                image_ref=None,
                diagnostics=None,
                metadata_json=meta,
            )
            assert artifact is not None
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
                "ecosystem.plugin_upgraded",
                "local_artifact",
                artifact.id,
                actor_type="admin" if admin_flag else "user",
                result={
                    "kind": kind,
                    "stable_id": stable_id,
                    "from_version": prev_version,
                    "version": version,
                    "approval_status": approval,
                },
            )
            db.commit()
        else:
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
                result={
                    "kind": kind,
                    "stable_id": stable_id,
                    "version": version,
                    "approval_status": approval,
                },
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
            zip_bytes=None,
            job_runner_client=job_runner_client,
            plugin_source_root=src_root,
            stage_root=stg_root,
            pip_index_url=settings_rec.pip_index_url,
            preextracted=(files, dirs),
            metadata=meta,
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
    payload["duplicate"] = False
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
    """管理员审批通过待审生态制品；防止构建中并发重复触发及已下架制品回流。"""
    require_admin(db, admin_user.id)
    try:
        artifact = ecosystem_domain.get_artifact(db, artifact_id)
        if artifact is None:
            raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
        if artifact.approval_status == "rejected":
            raise EcosystemConflictError("已拒绝制品不可直接审批通过", params={"artifact_id": artifact_id})
        if artifact.install_status == "disabled":
            raise EcosystemConflictError(
                "已下架制品不可直接重新审批发布", params={"artifact_id": artifact_id}
            )
        if artifact.install_status in ("building", "testing"):
            raise EcosystemConflictError(
                "制品正在构建或断网自检中，禁止并发重复触发构建",
                params={"artifact_id": artifact_id, "install_status": artifact.install_status},
            )
        if artifact.approval_status == "approved" and artifact.install_status == "ready":
            db.commit()
            return serialize_artifact(artifact)

        settings_rec = ecosystem_domain.get_settings(db)
        next_install = "ready" if artifact.kind == "device_model" else "building"
        updated = ecosystem_domain.update_artifact(
            db,
            artifact_id,
            approval_status="approved",
            install_status=next_install,
            approved_by=admin_user.id,
            approved_at=datetime.now(UTC),
            diagnostics=None,
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
            metadata=updated.metadata_json,
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
    """管理员仅可拒绝未发布制品（pending / build_failed）；已下架或已就绪制品须保留审批血缘。"""
    require_admin(db, admin_user.id)
    try:
        artifact = ecosystem_domain.get_artifact(db, artifact_id)
        if artifact is None:
            raise EcosystemArtifactNotFoundError("生态制品不存在", params={"artifact_id": artifact_id})
        if artifact.approval_status == "rejected":
            db.commit()
            return serialize_artifact(artifact)
        if artifact.install_status not in ("pending", "build_failed"):
            raise EcosystemConflictError(
                "仅待审批或构建失败的未发布制品允许 reject；已就绪、构建中或已下架制品须保留审批记录",
                params={
                    "artifact_id": artifact_id,
                    "approval_status": artifact.approval_status,
                    "install_status": artifact.install_status,
                },
            )
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
    """两阶段无状态在途保护卸载：停用下架防新引用 + 核对非终态 CalcSnapshot + delete_plugin_image。"""
    require_admin(db, admin_user.id)
    try:
        artifact = ecosystem_domain.get_artifact(db, artifact_id)
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

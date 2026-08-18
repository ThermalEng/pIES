"""安全工具:密码哈希、强度校验与会话令牌。

- 密码哈希:bcrypt 算法(直接使用 bcrypt 包,自动加盐,可安全往返)。
  说明:pyproject 依赖 passlib[bcrypt]>=1.7.4,但 passlib 1.7.4 与 bcrypt>=4.1
  不兼容(其 bcrypt 后端读取 bcrypt.__about__ 失败且校验路径异常),
  故此处直接调用 bcrypt 包 API,输出同为 $2b$ 前缀的标准 bcrypt 哈希,
  接口语义与 CONTRACT 第 2 节一致(hash_password/verify_password)。
- 密码强度:至少 8 位,含大写、小写与数字(任务要求)。
- 会话令牌:secrets 生成;入库前用 token_hash 做 SHA-256 摘要,
  库中不存明文令牌(泄露数据库也不可冒用)。
"""

from __future__ import annotations

import re
import secrets
from typing import Final

import bcrypt

from iesplan.core.idgen import sha256_hex

#: 密码最低长度
MIN_PASSWORD_LENGTH: Final[int] = 8
#: bcrypt 单次哈希成本因子
BCRYPT_ROUNDS: Final[int] = 12


def hash_password(password: str) -> str:
    """使用 bcrypt 哈希密码(每次调用自动加盐)。

    参数:
        password: 明文密码(UTF-8 编码后不超过 72 字节)。
    返回:
        bcrypt 哈希字符串($2b$ 前缀,含盐)。
    异常:
        ValueError: 密码为空或超长。
    """
    if not password:
        raise ValueError("密码不能为空")
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raise ValueError("密码 UTF-8 编码后不能超过 72 字节")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与哈希是否匹配。

    参数:
        password: 待校验明文。
        password_hash: hash_password 产出的 bcrypt 哈希。
    返回:
        匹配返回 True;哈希格式非法返回 False(不抛异常,避免信息泄漏)。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def check_password_strength(password: str) -> tuple[bool, str]:
    """检查密码强度。

    规则(任务约定):至少 8 位,且同时包含大写字母、小写字母与数字。
    说明:产品规范(04 §9.2 A 表 password_weak 文案)要求"至少 {min} 个字符并包含
    字母与数字",此处按任务要求强化为大小写字母与数字同时存在。

    参数:
        password: 待检查密码。
    返回:
        (ok, reason):ok 为是否通过;reason 为不通过原因的中文描述(ok 时为 "")。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"密码长度至少 {MIN_PASSWORD_LENGTH} 位"
    if not re.search(r"[a-z]", password):
        return False, "密码必须包含小写字母"
    if not re.search(r"[A-Z]", password):
        return False, "密码必须包含大写字母"
    if not re.search(r"\d", password):
        return False, "密码必须包含数字"
    return True, ""


def new_session_token() -> str:
    """生成新的会话令牌。

    返回:
        32 字节随机数的 URL 安全 base64 编码(约 43 字符)。
    """
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """计算会话令牌的存储哈希(明文不入库)。

    参数:
        token: new_session_token 产出的会话令牌。
    返回:
        SHA-256 十六进制摘要(64 字符)。
    """
    return sha256_hex(token.encode("utf-8"))

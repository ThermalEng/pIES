"""安全模块单元测试:bcrypt 哈希往返、密码强度、会话令牌哈希。"""

import pytest

from iesplan.core.security import (
    MIN_PASSWORD_LENGTH,
    check_password_strength,
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)


class TestPasswordHash:
    def test_hash_verify_roundtrip(self):
        h = hash_password("Abc12345!")
        assert h != "Abc12345!"  # 不存明文
        assert verify_password("Abc12345!", h) is True
        assert verify_password("WrongPass1", h) is False

    def test_hash_salts_are_random(self):
        h1 = hash_password("Abc12345!")
        h2 = hash_password("Abc12345!")
        assert h1 != h2  # 每次加盐不同

    def test_empty_password_rejected(self):
        with pytest.raises(ValueError):
            hash_password("")

    def test_verify_malformed_hash(self):
        assert verify_password("Abc12345!", "not-a-hash") is False
        assert verify_password("Abc12345!", "") is False


class TestPasswordStrength:
    """至少 8 位,含大小写与数字。"""

    def test_strong_passwords(self):
        for pwd in ["Abc12345", "Passw0rd!", "aB3cdefgh", "Xy9" + "z" * 10]:
            ok, reason = check_password_strength(pwd)
            assert ok is True, (pwd, reason)

    def test_too_short(self):
        ok, reason = check_password_strength("Ab1c")
        assert ok is False
        assert str(MIN_PASSWORD_LENGTH) in reason

    def test_missing_lowercase(self):
        ok, _ = check_password_strength("ABC12345")
        assert ok is False

    def test_missing_uppercase(self):
        ok, _ = check_password_strength("abc12345")
        assert ok is False

    def test_missing_digit(self):
        ok, _ = check_password_strength("Abcdefgh")
        assert ok is False

    def test_reason_mentions_defect(self):
        _, reason = check_password_strength("abcdefgh")
        assert "大写" in reason
        _, reason = check_password_strength("ABCDEFGH")
        assert "小写" in reason
        _, reason = check_password_strength("ABCDabcd")
        assert "数字" in reason


class TestSessionToken:
    def test_token_generation_and_hash(self):
        t1 = new_session_token()
        t2 = new_session_token()
        assert t1 != t2  # 随机
        assert len(token_hash(t1)) == 64  # sha256 十六进制
        assert token_hash(t1) != token_hash(t2)
        assert token_hash(t1) == token_hash(t1)  # 确定性

    def test_token_hash_deterministic(self):
        t = new_session_token()
        assert token_hash(t) == token_hash(t)

    def test_idgen_helpers(self):
        from iesplan.core.idgen import new_id, new_idempotency_key, sha256_hex

        assert new_id() != new_id()
        assert new_id("prj-").startswith("prj-")
        assert new_idempotency_key().startswith("idem-")
        assert len(sha256_hex(b"abc")) == 64
        assert sha256_hex(b"abc") == sha256_hex(b"abc")

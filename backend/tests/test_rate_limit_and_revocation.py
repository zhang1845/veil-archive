"""限流、令牌挂失与验证码加固的回归测试（迭代方案第 01、03、04 条）。

用内存假 Redis 替代真实缓存，因此完全离线运行。假实现覆盖了被测代码用到的
全部命令，并刻意支持"整体不可用"模式，用来验证缓存故障时的降级行为。
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException, Request
from redis.exceptions import ConnectionError as RedisConnectionError

from src.core import rate_limit
from src.core.rate_limit import (
    LOGIN_BY_ACCOUNT,
    RateLimitRule,
    check_rate_limit,
    client_identifier,
    guard_login,
    reset_rate_limit,
)


class FakeRedis:
    """够用的内存 Redis：支持计数、过期、读写删。

    ``available=False`` 时每个命令都抛出连接错误，用于验证降级路径。
    """

    def __init__(self, available: bool = True):
        self.available = available
        self.values: dict[str, str] = {}
        self.expiry: dict[str, float] = {}

    def _guard(self):
        if not self.available:
            raise RedisConnectionError("fake redis is down")

    def _purge(self, key: str):
        deadline = self.expiry.get(key)
        if deadline is not None and deadline <= time.time():
            self.values.pop(key, None)
            self.expiry.pop(key, None)

    def incr(self, key: str) -> int:
        self._guard()
        self._purge(key)
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    def expire(self, key: str, seconds: int) -> bool:
        self._guard()
        self.expiry[key] = time.time() + seconds
        return True

    def setex(self, key: str, seconds: int, value: str) -> bool:
        self._guard()
        self.values[key] = str(value)
        self.expiry[key] = time.time() + seconds
        return True

    def get(self, key: str):
        self._guard()
        self._purge(key)
        return self.values.get(key)

    def exists(self, key: str) -> int:
        self._guard()
        self._purge(key)
        return 1 if key in self.values else 0

    def ttl(self, key: str) -> int:
        self._guard()
        deadline = self.expiry.get(key)
        return int(deadline - time.time()) if deadline else -1

    def delete(self, *keys: str) -> int:
        self._guard()
        removed = 0
        for key in keys:
            removed += 1 if self.values.pop(key, None) is not None else 0
            self.expiry.pop(key, None)
        return removed


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr("src.core.redis_client.get_redis_client", lambda: client)
    monkeypatch.setattr("src.core.rate_limit.get_redis_client", lambda: client)
    monkeypatch.setattr("src.services.auth_service.get_redis_client", lambda: client)
    return client


def make_request(headers: dict[str, str] | None = None, host: str = "10.0.0.9") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [
            (key.lower().encode(), value.encode())
            for key, value in (headers or {}).items()
        ],
        "client": (host, 12345),
    }
    return Request(scope)


# --------------------------------------------------------------------------
# 限流
# --------------------------------------------------------------------------

def test_attempts_beyond_the_limit_are_rejected(fake_redis):
    rule = RateLimitRule("test", max_attempts=3, window_seconds=60)

    for _ in range(3):
        check_rate_limit(rule, "someone")

    with pytest.raises(HTTPException) as excinfo:
        check_rate_limit(rule, "someone")
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"] == "60"


def test_separate_identifiers_have_separate_budgets(fake_redis):
    rule = RateLimitRule("test", max_attempts=1, window_seconds=60)
    check_rate_limit(rule, "first")
    check_rate_limit(rule, "second")  # 不应受 first 影响
    with pytest.raises(HTTPException):
        check_rate_limit(rule, "first")


def test_window_expiry_restores_the_budget(fake_redis):
    rule = RateLimitRule("test", max_attempts=1, window_seconds=1)
    check_rate_limit(rule, "someone")
    with pytest.raises(HTTPException):
        check_rate_limit(rule, "someone")

    fake_redis.expiry[f"ratelimit:test:someone"] = time.time() - 1
    check_rate_limit(rule, "someone")


def test_successful_login_clears_the_account_counter(fake_redis):
    request = make_request()
    for _ in range(LOGIN_BY_ACCOUNT.max_attempts):
        guard_login(request, "victim")

    reset_rate_limit(LOGIN_BY_ACCOUNT, "victim")
    guard_login(request, "victim")  # 计数已清零，不应被拒


def test_login_is_limited_per_account_not_only_per_source(fake_redis):
    """换来源继续猜同一个账号，仍然会被账号维度挡住。"""
    for index in range(LOGIN_BY_ACCOUNT.max_attempts):
        guard_login(make_request(host=f"10.0.0.{index}"), "victim")

    with pytest.raises(HTTPException) as excinfo:
        guard_login(make_request(host="10.0.0.200"), "victim")
    assert excinfo.value.status_code == 429


def test_cache_outage_does_not_block_normal_logins(monkeypatch):
    """限流是防滥用措施，缓存故障时应放行而不是让全站登录失败。"""
    down = FakeRedis(available=False)
    monkeypatch.setattr("src.core.rate_limit.get_redis_client", lambda: down)

    rule = RateLimitRule("test", max_attempts=1, window_seconds=60)
    for _ in range(5):
        check_rate_limit(rule, "someone")


# --------------------------------------------------------------------------
# 来源识别
# --------------------------------------------------------------------------

def test_forwarded_header_is_preferred_behind_a_proxy():
    request = make_request({"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert client_identifier(request) == "203.0.113.7"


def test_direct_connection_falls_back_to_peer_address():
    assert client_identifier(make_request(host="198.51.100.4")) == "198.51.100.4"


# --------------------------------------------------------------------------
# 令牌挂失
# --------------------------------------------------------------------------

def test_revoked_token_is_rejected_before_it_expires(fake_redis, monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", "k" * 64)
    token = AuthService.create_access_token({"sub": "someone", "user_id": 3})

    assert AuthService.verify_token(token).username == "someone"

    assert AuthService.revoke_token(token) is True

    with pytest.raises(HTTPException) as excinfo:
        AuthService.verify_token(token)
    assert excinfo.value.status_code == 401
    assert "重新登录" in excinfo.value.detail


def test_revoking_one_token_leaves_other_sessions_signed_in(fake_redis, monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", "k" * 64)
    phone = AuthService.create_access_token({"sub": "someone", "user_id": 3})
    laptop = AuthService.create_access_token({"sub": "someone", "user_id": 3})

    AuthService.revoke_token(phone)

    with pytest.raises(HTTPException):
        AuthService.verify_token(phone)
    assert AuthService.verify_token(laptop).username == "someone"


def test_revocation_entry_expires_with_the_token(fake_redis, monkeypatch):
    """挂失记录只需保留到令牌自然过期，不会无限增长。"""
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", "k" * 64)
    token = AuthService.create_access_token({"sub": "someone", "user_id": 3})
    AuthService.revoke_token(token)

    key = next(k for k in fake_redis.values if k.startswith("auth:token:revoked:"))
    assert 0 < fake_redis.ttl(key) <= 120 * 60


# --------------------------------------------------------------------------
# 短信验证码
# --------------------------------------------------------------------------

def test_mock_code_is_random_rather_than_a_fixed_well_known_value(fake_redis, monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("SMS_PROVIDER", "mock")
    monkeypatch.delenv("SMS_MOCK_CODE", raising=False)

    codes = set()
    for index in range(5):
        fake_redis.values.clear()
        fake_redis.expiry.clear()
        codes.add(AuthService.send_sms_code(f"1380000000{index}")["dev_code"])

    assert "123456" not in codes or len(codes) > 1, "验证码不应固定为 123456"
    assert all(len(code) == 6 and code.isdigit() for code in codes)


def test_production_refuses_to_send_a_mock_code(fake_redis, monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SMS_PROVIDER", "mock")

    with pytest.raises(HTTPException) as excinfo:
        AuthService.send_sms_code("13800000000")
    assert excinfo.value.status_code == 503


def test_code_guessing_is_capped(fake_redis, monkeypatch):
    """六位验证码不能在有效期内被无限次穷举。"""
    from src.services.auth_service import SMS_CODE_MAX_ATTEMPTS, AuthService

    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("SMS_PROVIDER", "mock")
    monkeypatch.setenv("SMS_MOCK_CODE", "654321")
    AuthService.send_sms_code("13800000000")

    for _ in range(SMS_CODE_MAX_ATTEMPTS):
        with pytest.raises(HTTPException) as excinfo:
            AuthService.verify_sms_code("13800000000", "000000")
        assert excinfo.value.status_code == 400

    with pytest.raises(HTTPException) as excinfo:
        AuthService.verify_sms_code("13800000000", "654321")
    assert excinfo.value.status_code == 429


def test_correct_code_still_works_within_the_attempt_budget(fake_redis, monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("SMS_PROVIDER", "mock")
    monkeypatch.setenv("SMS_MOCK_CODE", "654321")
    AuthService.send_sms_code("13800000000")

    with pytest.raises(HTTPException):
        AuthService.verify_sms_code("13800000000", "111111")

    AuthService.verify_sms_code("13800000000", "654321")

    # 用过即焚：同一枚验证码不能重复使用。
    with pytest.raises(HTTPException):
        AuthService.verify_sms_code("13800000000", "654321")

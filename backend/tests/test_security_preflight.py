"""启动安全自检与令牌加固的回归测试（迭代方案第 01–04 条）。

这些用例锁定的是"不安全的配置必须启动失败"这一约定本身。它们全部离线运行：
不连数据库、不连缓存、不发网络请求。
"""
from __future__ import annotations

import time
from datetime import timedelta

import pytest
from fastapi import HTTPException
from jose import jwt

from src.core import security_preflight
from src.core.security_preflight import (
    InsecureConfigurationError,
    LEGACY_DEFAULT_SECRET_KEY,
    MINIMUM_SECRET_KEY_LENGTH,
    collect_configuration_problems,
    enforce_security_configuration,
    get_secret_key,
    is_production,
    secret_key_is_usable,
)

STRONG_KEY = "z" * 64


@pytest.fixture(autouse=True)
def clear_generated_key():
    """每个用例都从干净状态开始，避免共用上一次生成的临时密钥。"""
    security_preflight._generated_secret_key = None
    yield
    security_preflight._generated_secret_key = None


@pytest.fixture
def production(monkeypatch):
    """一份除被测项外全部合格的生产配置。"""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "120")
    monkeypatch.setenv("SMS_PROVIDER", "aliyun")
    monkeypatch.setenv("ALLOW_ANONYMOUS_ACCESS", "false")
    monkeypatch.setenv("INVITE_CODES", "real-invite-code")
    monkeypatch.setenv("FILE_STORAGE", "dir")
    monkeypatch.setenv("DB_PASSWORD", "a-long-random-database-password")
    monkeypatch.setenv("CORS_ORIGINS", "https://jubensha.example.cn")
    return monkeypatch


# --------------------------------------------------------------------------
# 密钥解析
# --------------------------------------------------------------------------

def test_legacy_default_secret_key_is_never_usable():
    """源码里那个公开的兜底密钥必须被判定为不可用。"""
    assert not secret_key_is_usable(LEGACY_DEFAULT_SECRET_KEY)
    assert not secret_key_is_usable("")
    assert not secret_key_is_usable("CHANGE_ME_TO_A_LONG_RANDOM_VALUE")
    assert not secret_key_is_usable("x" * (MINIMUM_SECRET_KEY_LENGTH - 1))
    assert secret_key_is_usable(STRONG_KEY)


def test_development_falls_back_to_ephemeral_key_not_the_public_default(monkeypatch):
    """开发环境缺省时用一次性随机密钥，绝不使用公开的写死值。"""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    key = get_secret_key()
    assert key != LEGACY_DEFAULT_SECRET_KEY
    assert len(key) >= MINIMUM_SECRET_KEY_LENGTH
    # 同一进程内保持稳定，否则刚签发的令牌立刻就验不过。
    assert get_secret_key() == key


def test_configured_strong_key_is_used_as_is(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    assert get_secret_key() == STRONG_KEY


def test_legacy_default_in_env_is_replaced_not_honored(monkeypatch):
    """即使有人把公开默认值显式写进 .env，也不会被采用。"""
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("SECRET_KEY", LEGACY_DEFAULT_SECRET_KEY)
    assert get_secret_key() != LEGACY_DEFAULT_SECRET_KEY


# --------------------------------------------------------------------------
# 生产自检
# --------------------------------------------------------------------------

def test_development_never_blocks_startup(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    assert collect_configuration_problems() == []
    enforce_security_configuration(reporter=lambda _: None)


def test_production_requires_env_marker(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    assert is_production()
    monkeypatch.setenv("ENV", "development")
    assert not is_production()


def test_fully_configured_production_passes(production):
    assert collect_configuration_problems() == []
    enforce_security_configuration(reporter=lambda _: None)


@pytest.mark.parametrize(
    "name,value,expected_fragment",
    [
        ("SECRET_KEY", "", "SECRET_KEY 未设置"),
        ("SECRET_KEY", LEGACY_DEFAULT_SECRET_KEY, "仍是源码或模板中的默认值"),
        ("SECRET_KEY", "short", "长度不足"),
        ("ACCESS_TOKEN_EXPIRE_MINUTES", "43200", "超过了生产上限"),
        ("ACCESS_TOKEN_EXPIRE_MINUTES", "abc", "不是合法的整数"),
        ("SMS_PROVIDER", "mock", "短信验证码是假的"),
        ("ALLOW_ANONYMOUS_ACCESS", "true", "必须关闭"),
        ("INVITE_CODES", "CHANGE_ME_INVITE_CODE", "模板占位值"),
        ("DB_PASSWORD", "password", "弱口令"),
        ("CORS_ORIGINS", "https://your-domain.example", "模板示例域名"),
    ],
)
def test_each_unsafe_production_setting_blocks_startup(
    production, name, value, expected_fragment
):
    production.setenv(name, value)

    problems = collect_configuration_problems()
    assert any(expected_fragment in problem for problem in problems), problems

    with pytest.raises(InsecureConfigurationError) as excinfo:
        enforce_security_configuration(reporter=lambda _: None)
    assert expected_fragment in str(excinfo.value)


def test_default_object_storage_credentials_block_startup(production):
    production.setenv("FILE_STORAGE", "minio")
    production.setenv("MINIO_ACCESS_KEY", "minioadmin")
    production.setenv("MINIO_SECRET_KEY", "minioadmin")

    problems = collect_configuration_problems()
    assert any("MINIO_ACCESS_KEY" in problem for problem in problems)
    assert any("MINIO_SECRET_KEY" in problem for problem in problems)


def test_local_storage_skips_object_storage_credentials(production):
    """改用本机存储时不该再要求对象存储凭据。"""
    production.setenv("FILE_STORAGE", "dir")
    production.setenv("MINIO_ACCESS_KEY", "minioadmin")
    assert collect_configuration_problems() == []


def test_all_problems_are_reported_at_once(production):
    """一次列全部问题，避免使用者修一条再被下一条挡住。"""
    production.setenv("SECRET_KEY", "")
    production.setenv("SMS_PROVIDER", "mock")
    production.setenv("ALLOW_ANONYMOUS_ACCESS", "true")

    problems = collect_configuration_problems()
    assert len(problems) >= 3

    with pytest.raises(InsecureConfigurationError) as excinfo:
        enforce_security_configuration(reporter=lambda _: None)
    message = str(excinfo.value)
    assert "SECRET_KEY" in message and "SMS_PROVIDER" in message and "ALLOW_ANONYMOUS_ACCESS" in message


def test_startup_runs_the_guard_before_touching_the_database(monkeypatch):
    """自检必须早于数据库初始化，不安全的部署不该连上数据库。"""
    from src.core import startup

    calls: list[str] = []

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.setattr(
        "src.core.dependency_container.configure_services",
        lambda: calls.append("configure_services"),
    )
    monkeypatch.setattr(
        "src.db.session.init_database",
        lambda: calls.append("init_database"),
    )

    with pytest.raises(InsecureConfigurationError):
        startup.initialize_application()

    assert calls == []


# --------------------------------------------------------------------------
# 令牌：编号、类型、有效期
# --------------------------------------------------------------------------

def test_access_token_carries_id_type_and_short_expiry(monkeypatch):
    from src.services.auth_service import ACCESS_TOKEN_EXPIRE_MINUTES, AuthService

    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    token = AuthService.create_access_token({"sub": "someone", "user_id": 7})
    claims = jwt.decode(token, STRONG_KEY, algorithms=["HS256"])

    assert claims["type"] == "access"
    assert claims["jti"]
    assert ACCESS_TOKEN_EXPIRE_MINUTES <= 24 * 60, "默认有效期不应超过一天"
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_EXPIRE_MINUTES * 60


def test_each_token_gets_a_distinct_identifier(monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    data = {"sub": "someone", "user_id": 7}
    first = jwt.decode(AuthService.create_access_token(data), STRONG_KEY, algorithms=["HS256"])
    second = jwt.decode(AuthService.create_access_token(data), STRONG_KEY, algorithms=["HS256"])
    assert first["jti"] != second["jti"]


def test_refresh_token_cannot_be_used_as_an_access_token(monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    monkeypatch.setattr(AuthService, "is_token_revoked", staticmethod(lambda jti: False))

    refresh = AuthService.create_refresh_token({"sub": "someone", "user_id": 7})
    with pytest.raises(HTTPException) as excinfo:
        AuthService.verify_token(refresh)
    assert excinfo.value.status_code == 401

    # 作为续期凭条使用时正常通过。
    assert AuthService.verify_token(refresh, expected_type="refresh").username == "someone"


def test_token_signed_with_the_public_default_key_is_rejected(monkeypatch):
    """用公开的历史默认密钥伪造的令牌必须验不过。"""
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    forged = jwt.encode(
        {"sub": "admin", "user_id": 1, "type": "access", "jti": "x",
         "exp": int(time.time()) + 3600},
        LEGACY_DEFAULT_SECRET_KEY,
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as excinfo:
        AuthService.verify_token(forged)
    assert excinfo.value.status_code == 401


def test_expired_token_is_rejected(monkeypatch):
    from src.services.auth_service import AuthService

    monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
    expired = AuthService.create_access_token(
        {"sub": "someone", "user_id": 7}, expires_delta=timedelta(seconds=-1)
    )
    with pytest.raises(HTTPException) as excinfo:
        AuthService.verify_token(expired)
    assert excinfo.value.status_code == 401
    assert "过期" in excinfo.value.detail


# --------------------------------------------------------------------------
# 鉴权白名单与真实入口必须对得上
# --------------------------------------------------------------------------

def test_refresh_endpoint_is_reachable_without_an_access_token():
    """续期凭条在请求体里，因此换发入口必须免鉴权。

    漏掉这条规则时，访问令牌一过期就再也换不出新的，使用者会被直接踢下线；
    这类"白名单与真实入口对不上"的问题只有端到端走一遍才会暴露，故单独锁定。
    """
    from src.core.auth_middleware import AuthLevel, UnifiedAuthMiddleware

    middleware = UnifiedAuthMiddleware(app=None)  # type: ignore[arg-type]
    assert middleware.get_auth_level("/api/auth/refresh", "POST") == AuthLevel.NONE
    # 其余受保护入口不受影响。
    assert middleware.get_auth_level("/api/auth/me", "GET") == AuthLevel.REQUIRED
    assert middleware.get_auth_level("/api/admin/fusion/authoring-jobs", "GET") == AuthLevel.ADMIN

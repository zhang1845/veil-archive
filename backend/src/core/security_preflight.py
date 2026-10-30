"""部署前安全自检：配置不安全时直接拒绝启动。

对应迭代方案第 02 条。此前多项安全配置都带有"忘了设也能跑"的默认值——
签名密钥有写死在代码里的公开兜底值，短信验证码默认是固定假码，对象存储
默认是出厂账号密码。任何一项被遗漏，程序都会照常启动，且不会有任何提示。

这里把这些默认值集中列为已知不安全值，并在应用初始化的第一步统一检查：

* 生产部署（``ENV=production``）任何一项不合格都会抛出
  :class:`InsecureConfigurationError`，进程不会进入可服务状态。
* 开发环境保持可用：缺失或仍为默认值的签名密钥会被替换成一次性随机值
  并打印警告，因此本机永远不会使用那个公开的写死密钥，重启后旧令牌自然失效。

本模块只读取环境变量，不连接数据库、缓存或网络，可以安全地在导入期调用。
"""
from __future__ import annotations

import os
import secrets
from typing import Callable

# 历史上写死在 auth_service 中的兜底密钥。它已随源码公开，必须视为已泄露。
LEGACY_DEFAULT_SECRET_KEY = "your-secret-key-change-this-in-production"

# 配置模板里留给使用者替换的占位符；原样保留即视为未配置。
PLACEHOLDER_MARKERS = ("CHANGE_ME", "your-secret-key", "changeme")

# 对象存储与数据库的出厂默认凭据。
INSECURE_STORAGE_CREDENTIALS = {"minioadmin", "minio", "admin", ""}
INSECURE_DB_PASSWORDS = {"password", "postgres", "123456", ""}

# 生产环境允许的访问令牌最长有效期。超过一天的长效令牌一旦泄露，
# 影响窗口过长；迭代方案第 04 条要求默认收敛到小时级。
PRODUCTION_MAX_TOKEN_MINUTES = 24 * 60

MINIMUM_SECRET_KEY_LENGTH = 32

_generated_secret_key: str | None = None


class InsecureConfigurationError(RuntimeError):
    """生产配置未达到最低安全要求，进程不得继续启动。"""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker.lower() in lowered for marker in PLACEHOLDER_MARKERS)


def is_production() -> bool:
    """是否为生产部署。

    只有显式写明 ``ENV=production`` 才算生产，避免把未配置的环境
    误判成生产而让开发无法启动；反过来，生产环境漏写这一项时，
    下面的检查会退化成开发模式的宽松规则，所以部署模板必须保留该项。
    """
    return _env("ENV", "development").lower() in {"production", "prod"}


def secret_key_is_usable(value: str) -> bool:
    """签名密钥是否可用于签发令牌。"""
    if not value or value == LEGACY_DEFAULT_SECRET_KEY:
        return False
    if _looks_like_placeholder(value):
        return False
    return len(value) >= MINIMUM_SECRET_KEY_LENGTH


def get_secret_key() -> str:
    """返回用于签发/校验令牌的密钥。

    生产环境必须由 ``SECRET_KEY`` 显式提供合格值，否则
    :func:`enforce_security_configuration` 会在启动时就拦下。
    开发环境允许缺省，此时返回一个进程内一次性随机密钥——
    它不会出现在任何源码或模板里，因此无法被离线伪造。
    """
    global _generated_secret_key

    configured = _env("SECRET_KEY")
    if secret_key_is_usable(configured):
        return configured

    if _generated_secret_key is None:
        _generated_secret_key = secrets.token_urlsafe(48)
        print(
            "⚠️ SECRET_KEY 未配置或仍为默认值，已改用本次启动的一次性随机密钥。\n"
            "   本机开发可继续使用；进程重启后既有登录会失效。\n"
            "   生产部署必须在 .env 中设置一个至少 "
            f"{MINIMUM_SECRET_KEY_LENGTH} 位的随机 SECRET_KEY。"
        )
    return _generated_secret_key


def _token_minutes() -> int:
    raw = _env("ACCESS_TOKEN_EXPIRE_MINUTES")
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return -1


def collect_configuration_problems() -> list[str]:
    """返回当前配置下的全部安全问题，每条都说明如何修复。

    始终检查全部项目并一次性返回，避免使用者修好一条又被下一条挡住。
    非生产环境返回空列表——开发的宽松处理由 :func:`get_secret_key` 承担。
    """
    if not is_production():
        return []

    problems: list[str] = []

    secret_key = _env("SECRET_KEY")
    if not secret_key:
        problems.append(
            "SECRET_KEY 未设置。它是签发登录令牌的密钥，缺失时程序会退回到"
            "源码里公开的默认值，任何人都能据此伪造管理员身份。"
            f"请设置一个至少 {MINIMUM_SECRET_KEY_LENGTH} 位的随机值，"
            "例如执行：python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    elif secret_key == LEGACY_DEFAULT_SECRET_KEY or _looks_like_placeholder(secret_key):
        problems.append(
            "SECRET_KEY 仍是源码或模板中的默认值，等同于未设置，必须替换为随机值。"
        )
    elif len(secret_key) < MINIMUM_SECRET_KEY_LENGTH:
        problems.append(
            f"SECRET_KEY 长度不足 {MINIMUM_SECRET_KEY_LENGTH} 位，容易被暴力破解，请重新生成。"
        )

    minutes = _token_minutes()
    if minutes < 0:
        problems.append("ACCESS_TOKEN_EXPIRE_MINUTES 不是合法的整数分钟数。")
    elif minutes > PRODUCTION_MAX_TOKEN_MINUTES:
        problems.append(
            f"ACCESS_TOKEN_EXPIRE_MINUTES={minutes} 超过了生产上限 "
            f"{PRODUCTION_MAX_TOKEN_MINUTES} 分钟。长效令牌一旦泄露，"
            "影响窗口过长；建议设为 120，并依赖续期凭条保持登录状态。"
        )

    sms_provider = _env("SMS_PROVIDER", "mock").lower()
    if sms_provider == "mock":
        problems.append(
            "SMS_PROVIDER=mock 表示短信验证码是假的（固定或随机生成后直接回显），"
            "任何人都能用它登录他人账号。生产环境必须接入真实短信供应商；"
            "尚未接入时请把手机登录整体关闭，而不是带着假验证码上线。"
        )

    if _env("ALLOW_ANONYMOUS_ACCESS", "false").lower() == "true":
        problems.append(
            "ALLOW_ANONYMOUS_ACCESS=true 会让任何访客直接以共享账户登录，生产环境必须关闭。"
        )

    invite_codes = _env("INVITE_CODES")
    if invite_codes and _looks_like_placeholder(invite_codes):
        problems.append("INVITE_CODES 仍是模板占位值，请替换为真实邀请码或留空。")

    if _env("FILE_STORAGE", "minio").lower() not in {"local", "dir"}:
        for name in ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
            value = _env(name)
            if value.lower() in INSECURE_STORAGE_CREDENTIALS or _looks_like_placeholder(value):
                problems.append(
                    f"{name} 仍是对象存储的出厂默认凭据，请改为专用账号密码。"
                )

    db_password = _env("DB_PASSWORD")
    if db_password.lower() in INSECURE_DB_PASSWORDS or _looks_like_placeholder(db_password):
        problems.append("DB_PASSWORD 是弱口令或模板占位值，请改为随机密码。")

    cors = _env("CORS_ORIGINS")
    if "your-domain.example" in cors:
        problems.append("CORS_ORIGINS 仍是模板示例域名，请改为真实前端域名。")

    return problems


def enforce_security_configuration(
    reporter: Callable[[str], None] = print,
) -> None:
    """在应用初始化最前面调用；生产配置不合格时抛出异常终止启动。"""
    problems = collect_configuration_problems()
    if not problems:
        if is_production():
            reporter("✅ 生产安全自检通过")
        return

    listed = "\n".join(f"  {index}. {text}" for index, text in enumerate(problems, 1))
    raise InsecureConfigurationError(
        "生产安全自检未通过，已拒绝启动。请修复下列问题后重试：\n"
        f"{listed}\n"
        "修复这些项之前，本服务不应对外开放。"
    )

"""接口限流。

对应迭代方案第 03 条。此前全站没有任何频次限制：登录密码可以一秒钟试
上万次，验证码也可以无限次索取，没有任何一层会拦。

实现采用 Redis 固定窗口计数，够用且开销极小：每个"标识 + 窗口"一个计数键，
首次计数时设置过期时间，窗口结束后自动消失，不需要额外清理。

限流标识同时包含来源地址和业务标识（例如手机号），因此换 IP 重试和换号
重试都会各自受限，不会出现"换一个就绕过"的情况。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from src.core.redis_client import RedisError, get_redis_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    """一条限流规则：``window_seconds`` 秒内最多 ``max_attempts`` 次。"""

    name: str
    max_attempts: int
    window_seconds: int

    def retry_hint(self) -> str:
        if self.window_seconds % 60 == 0:
            return f"{self.window_seconds // 60} 分钟"
        return f"{self.window_seconds} 秒"


# 登录类接口的默认限额。取值偏保守：正常使用者远达不到，
# 自动化穷举则会在很早就被挡住。
LOGIN_BY_ACCOUNT = RateLimitRule("login:account", max_attempts=5, window_seconds=300)
LOGIN_BY_CLIENT = RateLimitRule("login:client", max_attempts=20, window_seconds=300)
SMS_BY_PHONE = RateLimitRule("sms:phone", max_attempts=5, window_seconds=3600)
SMS_BY_CLIENT = RateLimitRule("sms:client", max_attempts=10, window_seconds=3600)
REFRESH_BY_CLIENT = RateLimitRule("refresh:client", max_attempts=60, window_seconds=300)


def client_identifier(request: Request) -> str:
    """识别请求来源。

    服务部署在反向代理之后，直接取连接地址会得到代理自身的地址，
    所有人会共用同一个额度。因此优先采信代理写入的 ``X-Forwarded-For``
    首段。该头部可被伪造，所以它只作为限流维度之一，永远不用于鉴权。
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def _key(rule: RateLimitRule, identifier: str) -> str:
    return f"ratelimit:{rule.name}:{identifier}"


def check_rate_limit(rule: RateLimitRule, identifier: str) -> None:
    """记一次尝试；超出限额时抛出 429。

    缓存不可用时放行并记录错误日志。限流是防滥用措施而非授权判断，
    让它在 Redis 故障时挡住全部正常登录，反而制造了更大的可用性风险。
    """
    if not identifier:
        return

    key = _key(rule, identifier)
    try:
        client = get_redis_client()
        attempts = client.incr(key)
        if attempts == 1:
            client.expire(key, rule.window_seconds)
    except RedisError:
        logger.error("限流计数不可用，本次放行；请检查 Redis", exc_info=True)
        return

    if attempts > rule.max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"操作过于频繁，请 {rule.retry_hint()}后再试",
            headers={"Retry-After": str(rule.window_seconds)},
        )


def reset_rate_limit(rule: RateLimitRule, identifier: str) -> None:
    """清空某个标识的计数，登录成功后调用，避免正常用户被历史失败拖累。"""
    if not identifier:
        return
    try:
        get_redis_client().delete(_key(rule, identifier))
    except RedisError:
        logger.warning("清除限流计数失败：%s", rule.name, exc_info=True)


def guard_login(request: Request, account: Optional[str]) -> None:
    """登录类接口的组合限流：来源与账号各自计数。"""
    check_rate_limit(LOGIN_BY_CLIENT, client_identifier(request))
    if account:
        check_rate_limit(LOGIN_BY_ACCOUNT, account)


def clear_login_guard(account: Optional[str]) -> None:
    if account:
        reset_rate_limit(LOGIN_BY_ACCOUNT, account)


def guard_sms(request: Request, phone: str) -> None:
    """验证码接口的组合限流：同一手机号和同一来源都有小时级上限。"""
    check_rate_limit(SMS_BY_CLIENT, client_identifier(request))
    check_rate_limit(SMS_BY_PHONE, phone)


def guard_refresh(request: Request) -> None:
    check_rate_limit(REFRESH_BY_CLIENT, client_identifier(request))

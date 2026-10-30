"""共享的 Redis 客户端。

此前每次发送验证码都会新建一个 Redis 连接，登录高峰时会不断建连。
限流（迭代方案第 03 条）和令牌吊销（第 04 条）都需要同一个缓存，
因此把连接集中到这里复用，并统一处理不可用时的降级策略。
"""
from __future__ import annotations

import os
from typing import Optional

from redis import Redis
from redis.exceptions import RedisError

_client: Optional[Redis] = None
_client_url: Optional[str] = None


def redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def get_redis_client() -> Redis:
    """返回进程内复用的 Redis 客户端。

    连接参数变化时会重建客户端，方便测试切换到独立实例。
    这里只创建客户端对象，不会立刻建立网络连接。
    """
    global _client, _client_url

    url = redis_url()
    if _client is None or _client_url != url:
        _client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
        _client_url = url
    return _client


def reset_redis_client() -> None:
    """丢弃已缓存的客户端，供测试在切换配置后调用。"""
    global _client, _client_url
    _client = None
    _client_url = None


__all__ = ["RedisError", "get_redis_client", "redis_url", "reset_redis_client"]

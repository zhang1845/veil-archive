"""鉴权白名单必须与真实注册的接口对得上（迭代方案第 09 条）。

中间件用一张按优先级排序的规则表决定每个路径要不要鉴权。这张表是手写的，
路由却分散在二十多个模块里，两者很容易慢慢走偏，而且偏了没有任何迹象：

* **规则写了、接口不存在**——现在无害，但哪天有人加上同名接口，它会直接以免鉴权
  状态上线。本仓库就有过 ``/api/tts/synthesize`` 免登录、而该接口并不存在的情况；
  它是一个付费接口，真被加回来等于对公网开放免费额度。
* **方法写错**——例如白名单放行 POST，实际接口是 GET。豁免不生效，真正要放行的
  请求反而被挡住。访问令牌续期接口就因此一度完全不可用：凭条在请求体里，
  被默认规则拦在门外，令牌过期后再也换不回来。

因此这里把"每条豁免规则都必须命中一个真实接口"变成自动检查，不再依赖人记得。
只检查 ``/api/`` 下的规则：``/docs``、``/static`` 等由框架或挂载点提供，
是否存在取决于运行配置，不适合用同一把尺子衡量。
"""
from __future__ import annotations

import os
import re

import pytest

from src.core.auth_middleware import AuthLevel, AuthRule, UnifiedAuthMiddleware


def _all_registered_routes() -> list[tuple[str, frozenset[str]]]:
    """收集全部可能注册的接口，含按开关条件注册的那些。

    媒体相关路由只有 ENABLE_MEDIA_FEATURES=true 时才挂载，遗留管理接口只有
    ENABLE_LEGACY_ADMIN=true 时才挂载。白名单应当以"所有可能存在的接口"为准来
    校验，否则关掉某个开关时就看不出对应的规则是否已经失效。
    """
    from src.core.server import app
    from src.api.routes.character_routes import router as character_router
    from src.api.routes.evidence_routes import router as evidence_router
    from src.api.routes.image_generation_routes import router as image_router
    from src.api.routes.location_routes import router as location_router
    from src.api.routes.script_editor_routes import router as script_editor_router
    from src.api.routes.script_routes import router as script_router
    from src.api.routes.tts_routes import router as tts_router

    collected: list[tuple[str, frozenset[str]]] = []
    seen: set[tuple[str, frozenset[str]]] = set()

    def add(path: str, methods) -> None:
        entry = (path, frozenset(methods or {"GET"}))
        if entry not in seen:
            seen.add(entry)
            collected.append(entry)

    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            add(path, getattr(route, "methods", None))

    conditional_routers = (
        tts_router, image_router,                       # ENABLE_MEDIA_FEATURES
        script_router, script_editor_router, evidence_router,
        character_router, location_router,               # ENABLE_LEGACY_ADMIN
    )
    for router in conditional_routers:
        for route in router.routes:
            path = getattr(route, "path", None)
            if path:
                add(path, getattr(route, "methods", None))

    return collected


def _concrete_path(template: str) -> str:
    """把 ``/api/x/{id}`` 变成一个可用于匹配规则的具体路径。"""
    return re.sub(r"\{[^}]+\}", "1", template)


def _api_rules() -> list[AuthRule]:
    middleware = UnifiedAuthMiddleware(app=None)  # type: ignore[arg-type]
    return [rule for rule in middleware.auth_rules if rule.pattern.pattern.startswith("^/api/")]


def _rule_id(rule: AuthRule) -> str:
    return f"{rule.pattern.pattern} [{','.join(sorted(rule.methods))}]"


# 通配的兜底规则本来就不对应单个接口，逐条校验没有意义。
CATCH_ALL_PATTERNS = {r"^/api/.*"}


@pytest.mark.parametrize(
    "rule",
    [rule for rule in _api_rules() if rule.pattern.pattern not in CATCH_ALL_PATTERNS],
    ids=_rule_id,
)
def test_every_api_rule_matches_a_real_route(rule: AuthRule) -> None:
    """每条 /api 规则都必须命中至少一个真实接口，且方法要对得上。

    失败通常意味着两件事之一：接口被删/改名后规则没跟着删，或者规则里的
    HTTP 方法写错了。两种都要处理——前者是将来的安全隐患，后者是当下的功能故障。
    """
    routes = _all_registered_routes()

    path_matches = [
        (path, methods) for path, methods in routes
        if rule.pattern.match(_concrete_path(path))
    ]
    assert path_matches, (
        f"白名单规则 {_rule_id(rule)} 没有匹配到任何已注册接口。\n"
        "接口如果已经删除或改名，请同时删掉这条规则；否则将来有人加回同名接口时，"
        "它会直接以这条规则声明的鉴权级别上线。"
    )

    method_matches = [
        path for path, methods in path_matches
        if methods & {method.upper() for method in rule.methods}
    ]
    assert method_matches, (
        f"白名单规则 {_rule_id(rule)} 匹配到了接口 "
        f"{[path for path, _ in path_matches]}，但 HTTP 方法对不上：\n"
        f"实际方法为 {sorted(set().union(*(methods for _, methods in path_matches)))}。\n"
        "方法写错时这条规则不会生效，真正该放行的请求会被默认规则挡住。"
    )


def test_public_api_rules_are_an_explicit_short_list() -> None:
    """免鉴权的 /api 接口必须逐条列举在这里。

    新增一条免登录接口就会让本用例失败，从而强制在评审中被看到——
    免鉴权接口是对公网开放的入口，不应该悄悄多出来一个。
    """
    expected = {
        # 登录相关：请求发生在拿到令牌之前，只能免鉴权。
        ("^/api/auth/register", "POST"),
        ("^/api/auth/login", "POST"),
        ("^/api/auth/sms-code", "POST"),
        ("^/api/auth/phone-login", "POST"),
        ("^/api/auth/anonymous-login", "POST"),
        # 续期凭条在请求体里而非 Authorization 头里。
        ("^/api/auth/refresh", "POST"),
        # 公开的剧本浏览。
        ("^/api/scripts/public/?$", "GET"),
        ("^/api/scripts/search/?$", "GET"),
        # 音色列表只是静态清单，不产生费用。语音合成本身不在此列：它要花钱，
        # 必须登录，并且要和费用上限、限流一起设计后才谈得上开放。
        ("^/api/tts/voices", "GET"),
    }

    actual = {
        (rule.pattern.pattern, method)
        for rule in _api_rules()
        if rule.auth_level == AuthLevel.NONE
        for method in rule.methods
    }

    added = actual - expected
    removed = expected - actual
    assert not added, (
        f"新增了免鉴权接口：{sorted(added)}。\n"
        "确认它确实必须对未登录用户开放、且不会产生费用后，再把它加进本用例的清单。"
    )
    assert not removed, (
        f"以下免鉴权规则已不存在：{sorted(removed)}。\n"
        "如果是有意收紧权限，请同步更新本用例的清单。"
    )


# --------------------------------------------------------------------------
# 遗留管理接口的注册开关（迭代方案第 10 条）
# --------------------------------------------------------------------------

LEGACY_PREFIXES = (
    "/api/scripts",
    "/api/script-editor",
    "/api/evidence",
    "/api/characters",
    "/api/locations",
)


def _registered_legacy_paths(env: dict[str, str]) -> list[str]:
    """在给定环境变量下重新装配应用，返回其中的遗留接口路径。

    路由注册发生在模块导入期，因此必须清掉已缓存的模块再重新导入，
    否则读到的永远是本进程第一次导入时的那份配置。
    """
    import importlib
    import sys

    saved = {name: os.environ.get(name) for name in env}
    removed = [name for name in list(sys.modules) if name.startswith("src.core.server")]
    saved_modules = {name: sys.modules.pop(name) for name in removed}
    try:
        os.environ.update(env)
        server = importlib.import_module("src.core.server")
        return [
            path for route in server.app.routes
            if (path := getattr(route, "path", "")).startswith(LEGACY_PREFIXES)
        ]
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        sys.modules.update(saved_modules)


def test_development_keeps_legacy_admin_endpoints_available():
    """本机开发默认保留旧管理页面依赖的接口，不打断现有工作流。"""
    paths = _registered_legacy_paths({"ENV": "development", "SECRET_KEY": "d" * 64})
    assert paths, "开发环境应当仍然注册遗留管理接口"


def test_production_does_not_register_legacy_admin_endpoints_by_default():
    """生产环境默认不注册遗留接口。

    它们返回全知视角的剧本数据（含凶手身份与他人私本），属于早期全 AI 模拟器
    与旧编辑器的遗物，玩家链路一律走 /api/fusion。此前它们只被中间件的
    ADMIN 规则挡着，但十几个入口仍然存在；不注册意味着这些入口根本不存在。
    """
    paths = _registered_legacy_paths({"ENV": "production", "SECRET_KEY": "p" * 64})
    assert paths == [], f"生产环境不应注册遗留管理接口，但仍有：{paths}"


def test_production_can_opt_back_in_explicitly():
    """确需在生产排查问题时，可以显式开启——但必须是明确的决定。"""
    paths = _registered_legacy_paths(
        {"ENV": "production", "SECRET_KEY": "p" * 64, "ENABLE_LEGACY_ADMIN": "true"}
    )
    assert paths, "显式设置 ENABLE_LEGACY_ADMIN=true 时应当重新注册"

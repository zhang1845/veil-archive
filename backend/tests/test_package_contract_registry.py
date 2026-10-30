"""契约版本退役规则的强制检查（迭代方案第 07 条）。

这些用例的作用不是描述现状，而是让"版本只增不减"变得不可能悄悄发生：
新增一个契约版本却不登记、登记内容与代码实际支持情况对不上、或者写不出
退役下一步，都会在这里失败。
"""
from __future__ import annotations

import pytest

from src.schemas import script_package
from src.schemas.package_contract_registry import (
    CONTRACT_REGISTRY,
    UnknownContractVersion,
    authoring_contract_versions,
    contract_record,
    operative_contract_version,
    playable_contract_versions,
    retirement_plan,
)


def _versions_known_to_the_parser() -> set[str]:
    """从解析器实际支持的版本反推，避免登记表与代码各说各话。"""
    return {
        value for name, value in vars(script_package).items()
        if name.startswith("CONTRACT_VERSION") and isinstance(value, str)
    }


# --------------------------------------------------------------------------
# 登记表必须覆盖代码现状
# --------------------------------------------------------------------------

def test_every_implemented_version_is_registered():
    """代码里支持的每个版本都必须登记。

    这是本文件的核心约定：新增一版契约时如果忘了登记，这条会立刻失败，
    从而强制作者写明它取代了谁、旧版本怎么下线。
    """
    unregistered = _versions_known_to_the_parser() - {r.version for r in CONTRACT_REGISTRY}
    assert not unregistered, (
        f"以下契约版本已在代码中实现但没有登记：{sorted(unregistered)}。\n"
        "请在 src/schemas/package_contract_registry.py 中补上登记条目。"
    )


def test_registry_has_no_phantom_versions():
    """登记为仍在使用的版本，代码里必须真的还在。"""
    implemented = _versions_known_to_the_parser()
    live = {r.version for r in CONTRACT_REGISTRY if not r.fully_retired}
    missing = live - implemented
    assert not missing, (
        f"以下版本登记为仍在使用，但代码里找不到：{sorted(missing)}。\n"
        "如果实现已经删除，请把 authoring 和 play 都改为 ABSENT。"
    )


def test_authoring_support_matches_the_compiler():
    """登记的"可编译产出"必须与编译链路实际接受的版本一致。

    这一栏写错会直接误导下一个人：以为可以用最新版本编译，实际根本走不通。
    """
    from src.fusion import authoring_model

    # Step 是字面量类型而不是枚举，编译步骤的取值就是字符串 "COMPILE"。
    actual: set[str] = set()
    for version in _versions_known_to_the_parser():
        try:
            authoring_model._schema("COMPILE", version)
        except Exception:
            continue
        actual.add(version)

    assert set(authoring_contract_versions()) == actual, (
        f"登记的可编译版本 {sorted(authoring_contract_versions())} "
        f"与编译链路实际支持的 {sorted(actual)} 不一致。"
    )


# --------------------------------------------------------------------------
# 退役规则
# --------------------------------------------------------------------------

def test_every_older_version_names_its_successor():
    """除最新版本外都必须写明被谁取代，且后继本身也要登记。

    只写"废弃"而不写去向，等于把判断成本留给下一个人。
    """
    registered = {r.version for r in CONTRACT_REGISTRY}
    newest = CONTRACT_REGISTRY[-1]
    for record in CONTRACT_REGISTRY:
        if record is newest:
            assert record.superseded_by is None, f"{record.version} 是最新版本，不应有后继"
            continue
        assert record.superseded_by, f"{record.version} 必须写明后继版本"
        assert record.superseded_by in registered, (
            f"{record.version} 的后继 {record.superseded_by} 本身没有登记"
        )


def test_successor_chain_reaches_the_newest_version():
    """顺着后继一路走必须走到最新版本，不能断链或成环。"""
    newest = CONTRACT_REGISTRY[-1].version
    for record in CONTRACT_REGISTRY:
        seen = [record.version]
        cursor = record
        while cursor.superseded_by is not None:
            assert cursor.superseded_by not in seen, f"后继关系成环：{seen}"
            seen.append(cursor.superseded_by)
            cursor = contract_record(cursor.superseded_by)
        assert cursor.version == newest, (
            f"{record.version} 的后继链止于 {cursor.version}，没有走到最新版本 {newest}"
        )


def test_every_record_explains_itself_and_its_next_step():
    """保留理由和退役下一步都必须写具体。

    理由写得含糊、又没有下一步，版本就会一直留着——这正是积累到五个的原因。
    """
    for record in CONTRACT_REGISTRY:
        assert len(record.rationale) >= 30, f"{record.version} 的保留理由过于简略"
        assert record.rationale.strip() not in ("兼容", "保留兼容", "历史原因")
        assert len(record.retirement_next_step) >= 20, (
            f"{record.version} 没有写明退役下一步；"
            "没有下一步的版本不会自己消失。"
        )


def test_frozen_versions_stay_readable():
    """已冻结的版本仍要能读，否则存量数据会直接打不开。"""
    for record in CONTRACT_REGISTRY:
        if record.play == "FROZEN":
            assert record.is_playable, f"{record.version} 标为冻结就不该同时不可读"
        if record.authoring == "FROZEN":
            assert not record.accepts_new_packages


def test_at_least_one_version_can_both_author_and_play():
    """必须始终存在一个既能编译产出、又能游玩的版本。

    否则新内容无法产生——冻结版本时最容易踩的就是这个坑。
    """
    operative = operative_contract_version()
    record = contract_record(operative)
    assert record.authoring == "SUPPORTED" and record.play == "SUPPORTED"


def test_newest_version_without_authoring_path_is_recorded_as_such():
    """最新版本如果还不能编译产出，必须在登记表里如实写明。

    当前 1.3 / 1.4 只有游玩侧实现，实际产出仍靠 1.2。这个分裂是五版本并存
    无法收敛的直接原因，不能被"用最新版就好"的印象掩盖。
    """
    newest = CONTRACT_REGISTRY[-1]
    if newest.authoring != "SUPPORTED":
        assert "编译产出" in newest.retirement_next_step, (
            "最新版本缺少编译产出路径时，退役下一步必须指出这一点"
        )
        assert operative_contract_version() != newest.version


def test_unknown_version_is_rejected_with_actionable_guidance():
    """未登记的版本必须报错，并告诉调用者去哪里补登记。"""
    with pytest.raises(UnknownContractVersion) as excinfo:
        contract_record("script-package/9.9")
    assert "package_contract_registry" in str(excinfo.value)


def test_retirement_plan_is_printable():
    """退役表要能直接打印，方便排查和写文档时引用。"""
    text = retirement_plan()
    for record in CONTRACT_REGISTRY:
        assert record.version in text
    assert operative_contract_version() in text


def test_playable_versions_cover_the_runtime():
    """可游玩清单不能漏掉运行时实际会分派到的版本。"""
    playable = set(playable_contract_versions())
    # package_runtime 明确按这三个版本分派开局。
    for version in ("script-package/1.2", "script-package/1.3", "script-package/1.4"):
        assert version in playable, f"{version} 运行时可开局，但登记为不可游玩"

"""剧本包契约版本的登记表与退役规则（迭代方案第 07 条）。

此前每上一版新玩法都会把旧版本原样留着"以防万一"，于是 1.0 到 1.4 五个契约
同时活着。代价是改任何一处都要同时照顾五种旧情况，而且没有任何地方写明
哪个版本还该用、哪个只是为了读旧数据而保留——判断只能靠翻代码和记忆。

这里把这件事变成显式声明。登记一个版本需要说清两件独立的事：

**能不能编译产出**（``authoring``）
    管理员的编译链路能否以该版本生成新候选。由 ``authoring_model._schema``
    实际支持的集合决定。

**能不能游玩**（``play``）
    运行时能否按该版本开局。由 package_runtime / package_play 的分派决定。

两者必须分开记录，因为本项目当前它们并不重合：编译链路停在 1.2，而 1.3、1.4
只有游玩侧实现。也就是说**最新的两个契约没有内容产出路径**，该版本的包目前
只能由代码里的注册模块（``package_*_registration``）手工构造，仅测试在用。
这一点如果不写下来，很容易误以为"新候选应该用最新版本"。

状态取值：

``SUPPORTED``
    该侧完全支持。
``FROZEN``
    只能读取/继续已有数据，不再接受新产生的数据。这是退役的第一步。
``ABSENT``
    该侧从未实现，或实现已删除。

退役的完整路径是：编译侧先 FROZEN（停止产生新的旧版本数据）→ 存量随时间见底
→ 游玩侧 FROZEN → 确认没有存量后删代码。中间那一步必须依据真实数据，
不能凭印象跳过。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Support = Literal["SUPPORTED", "FROZEN", "ABSENT"]


@dataclass(frozen=True)
class ContractRecord:
    """一个契约版本的登记条目。"""

    version: str
    #: 编译链路能否以该版本产出新候选。
    authoring: Support
    #: 运行时能否按该版本开局。
    play: Support
    #: 后继版本；最新版本没有后继。
    superseded_by: str | None
    #: 为什么还留着它，或者当初为什么退役。写给将来的人看，不要只写"兼容"。
    rationale: str
    #: 下一步该做什么才能让它真正退役。没有明确下一步的版本会一直留着。
    retirement_next_step: str

    @property
    def accepts_new_packages(self) -> bool:
        return self.authoring == "SUPPORTED"

    @property
    def is_playable(self) -> bool:
        return self.play in ("SUPPORTED", "FROZEN")

    @property
    def fully_retired(self) -> bool:
        return self.authoring == "ABSENT" and self.play == "ABSENT"


CONTRACT_REGISTRY: tuple[ContractRecord, ...] = (
    ContractRecord(
        version="script-package/1.0",
        authoring="ABSENT",
        play="FROZEN",
        superseded_by="script-package/1.1",
        rationale=(
            "最初的阶段演练契约，只覆盖阶段推进与材料解锁，没有调查点也没有 AI 问答。"
            "编译链路从未支持它；仅 package_flow 的阶段演练还能读取早期数据。"
        ),
        retirement_next_step=(
            "确认真实库中没有 1.0 的候选后，删除 package_flow 与 package_flow_rules "
            "中的 1.0 分支及 ScriptPackage 模型。它是五个版本里最容易先清掉的一个。"
        ),
    ),
    ContractRecord(
        version="script-package/1.1",
        authoring="SUPPORTED",
        play="FROZEN",
        superseded_by="script-package/1.2",
        rationale=(
            "在 1.0 上补齐了分享解锁与权限条件，但没有共享调查点与费用记账。"
            "它目前仍是 authoring_routes 的默认编译契约——这是历史遗留，"
            "新候选应当显式选择 1.2。"
        ),
        retirement_next_step=(
            "把 authoring_routes 的默认契约从 1.1 改为 1.2，之后将本条 authoring "
            "改为 FROZEN。改默认值会影响正在进行的编译任务，需显式决定切换时点。"
        ),
    ),
    ContractRecord(
        version="script-package/1.2",
        authoring="SUPPORTED",
        play="SUPPORTED",
        superseded_by="script-package/1.3",
        rationale=(
            "首个带共享调查点、固定动作成本与授权材料奖励的契约，也是审核与发布"
            "链路长期使用的版本。它是当前唯一既能编译产出、又能正常游玩的契约，"
            "因此实际承担产出的版本是它，而不是版本号最大的 1.4。"
        ),
        retirement_next_step=(
            "在 1.3/1.4 具备编译产出路径之前不能冻结——冻结它会导致没有任何版本"
            "可以产生新的可玩候选。"
        ),
    ),
    ContractRecord(
        version="script-package/1.3",
        authoring="ABSENT",
        play="SUPPORTED",
        superseded_by="script-package/1.4",
        rationale=(
            "在 1.2 上增加了分阶段记忆触发，只有游玩侧实现。该版本的包目前只能由"
            "代码中的注册模块手工构造，编译链路无法产出，因此尚无真实存量。"
        ),
        retirement_next_step=(
            "要么为它补上编译产出路径，要么确认 1.4 已完全覆盖其能力后直接删除——"
            "没有存量数据，是五个版本中删除代价最低的一个。"
        ),
    ),
    ContractRecord(
        version="script-package/1.4",
        authoring="ABSENT",
        play="SUPPORTED",
        superseded_by=None,
        rationale=(
            "最新契约：引导式流程与完整整局机制（五席封卷、结算与恢复）。"
            "同样只有游玩侧实现，内容只能手工构造，目前仅测试夹具在用。"
        ),
        retirement_next_step=(
            "补上编译产出路径，使它能成为真正的当前版本；在那之前 1.2 仍承担"
            "全部实际产出，五版本并存的局面无法收敛。"
        ),
    ),
)


_BY_VERSION = {record.version: record for record in CONTRACT_REGISTRY}


class UnknownContractVersion(ValueError):
    """出现了没有登记的契约版本。"""


def contract_record(version: str) -> ContractRecord:
    """按版本号取登记条目；未登记的版本视为错误而不是默默放行。"""
    try:
        return _BY_VERSION[version]
    except KeyError:
        raise UnknownContractVersion(
            f"契约版本 {version!r} 没有登记。新增版本时必须同时在 "
            "src/schemas/package_contract_registry.py 中登记它的编译/游玩支持情况、"
            "后继版本、保留理由和退役下一步。"
        ) from None


def authoring_contract_versions() -> tuple[str, ...]:
    """编译链路可以产出的版本。"""
    return tuple(r.version for r in CONTRACT_REGISTRY if r.authoring == "SUPPORTED")


def playable_contract_versions() -> tuple[str, ...]:
    """运行时可以开局的版本。"""
    return tuple(r.version for r in CONTRACT_REGISTRY if r.is_playable)


def operative_contract_version() -> str:
    """实际承担新内容产出的版本：既能编译产出、又能游玩的那个最新版本。

    它未必是版本号最大的那个——这正是当前的情况，也是必须显式算出来而不是
    默认"用最新版"的原因。
    """
    usable = [
        r.version for r in CONTRACT_REGISTRY
        if r.authoring == "SUPPORTED" and r.play == "SUPPORTED"
    ]
    if not usable:
        raise AssertionError(
            "没有任何契约版本既能编译产出又能游玩；新内容将无法产生。"
        )
    return usable[-1]


def retirement_plan() -> str:
    """给人看的退役表，可用于文档或排查时打印。"""
    header = f"{'版本':<22}{'编译':<12}{'游玩':<12}{'后继':<22}"
    rows = [header, "-" * len(header)]
    for record in CONTRACT_REGISTRY:
        rows.append(
            f"{record.version:<22}{record.authoring:<12}{record.play:<12}"
            f"{record.superseded_by or '—':<22}"
        )
    rows.append("")
    rows.append(f"实际产出版本：{operative_contract_version()}")
    return "\n".join(rows)

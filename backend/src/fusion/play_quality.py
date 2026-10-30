"""对局质量评分：把"AI 演得好不好"变成可比较的数字（迭代方案第 12 条）。

此前判断 AI 演得好不好只能靠人一局一局试玩，慢、贵，而且说不清这次改动到底
是变好还是变坏——调一版话术要花真钱，结论却只是"感觉好一点"。

这里对一局已经结束（或进行中）的对局做纯离线统计，不调用任何模型，也不读取
数据库：输入是这局已经产生的记录，输出是一张可直接对比的成绩单。

四项指标对应作者记录的实际毛病：

``required_speech``
    必讲要点讲完了没有，以及其中有多少是 AI 自己讲的、多少是程序代述的。
    代述能保证内容不丢，但代述比例高说明 AI 本身不可靠——这正是"该说的话
    会漏说"在数字上的样子。
``repeated_calls``
    同一对角色在同一阶段重复通话的次数。规则引擎已经拦截，因此这项应当为 0；
    一旦不为 0，说明有绕过拦截的路径。
``duplicate_statements``
    近似重复的公开发言。AI 反复说同一件事是"表现忽好忽坏"最常见的表现形式。
``out_of_scope``
    发言里出现了本角色无权知道的材料标识。这是最严重的一类，因为它直接破坏
    推理的公平性。

评分不替代人工试玩，它只是让"这次改动有没有让情况变好"有一个客观答案。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata


def _normalize(text: str) -> str:
    """按语义比较发言：忽略空白、标点和全角半角差异。"""
    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s\W_]+", "", folded, flags=re.UNICODE).lower()


@dataclass(frozen=True)
class RequiredSpeechScore:
    total: int
    spoken_by_ai: int
    spoken_by_engine: int
    missing: tuple[str, ...]

    @property
    def covered(self) -> int:
        return self.spoken_by_ai + self.spoken_by_engine

    @property
    def coverage(self) -> float:
        """0.0–1.0。没有必讲要点时记为 1.0，避免把"无要求"判成不及格。"""
        return 1.0 if self.total == 0 else self.covered / self.total

    @property
    def ai_share(self) -> float:
        """已讲内容里 AI 自己讲的比例；代述占比越高，说明 AI 越不可靠。"""
        return 1.0 if self.covered == 0 else self.spoken_by_ai / self.covered


@dataclass(frozen=True)
class PlayQualityReport:
    required_speech: RequiredSpeechScore
    repeated_calls: int
    duplicate_statements: int
    out_of_scope: tuple[str, ...]
    statements: int

    @property
    def clean(self) -> bool:
        """是否没有任何已知问题。作为回归基线使用。"""
        return (
            self.required_speech.coverage == 1.0
            and self.repeated_calls == 0
            and self.duplicate_statements == 0
            and not self.out_of_scope
        )

    def render(self) -> str:
        score = self.required_speech
        lines = [
            "对局质量成绩单",
            "-" * 40,
            f"公开发言总数        {self.statements}",
            f"必讲要点            {score.covered}/{score.total}"
            f"（AI 自述 {score.spoken_by_ai}，程序代述 {score.spoken_by_engine}）",
        ]
        if score.missing:
            lines.append(f"  仍未讲出           {', '.join(score.missing)}")
        lines += [
            f"重复通话            {self.repeated_calls}",
            f"近似重复发言        {self.duplicate_statements}",
            f"越权提及            {len(self.out_of_scope)}"
            + (f"（{', '.join(self.out_of_scope)}）" if self.out_of_scope else ""),
            "-" * 40,
            "结论：无已知问题" if self.clean else "结论：存在上列问题",
        ]
        return "\n".join(lines)


def _score_required_speech(required, engine_spoken, ai_spoken) -> RequiredSpeechScore:
    engine_keys = {(e["collection"], e["material_id"], e["owner"]) for e in engine_spoken}
    ai_keys = {(a["collection"], a["material_id"], a["owner"]) for a in ai_spoken}

    by_engine = by_ai = 0
    missing: list[str] = []
    for item in required:
        key = (item["collection"], item["material_id"], item["owner_character_id"])
        if key in ai_keys:
            # AI 自己讲过就算它的，即使程序后来又代述了一次。
            by_ai += 1
        elif key in engine_keys:
            by_engine += 1
        else:
            missing.append(item["material_id"])

    return RequiredSpeechScore(
        total=len(required),
        spoken_by_ai=by_ai,
        spoken_by_engine=by_engine,
        missing=tuple(missing),
    )


def _count_repeated_calls(calls) -> int:
    """同一阶段里同一对角色接通的重复次数。

    按无序对统计：a 打给 b 和 b 打给 a 属于同一对人再次通话。
    """
    seen: set[tuple[str, frozenset[str]]] = set()
    repeats = 0
    for call in calls:
        key = (call["phase_id"], frozenset({call["caller"], call["callee"]}))
        if key in seen:
            repeats += 1
        else:
            seen.add(key)
    return repeats


def _count_duplicate_statements(statements) -> int:
    """同一角色近似重复的公开发言数（第二次及以后各记一次）。"""
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for entry in statements:
        key = (entry["speaker"], _normalize(entry["text"]))
        if not key[1]:
            continue
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _find_out_of_scope(statements, permitted) -> tuple[str, ...]:
    """发言里出现了本角色无权知道的材料标识。

    只匹配材料标识这种确定性信号，不做语义判断——评分必须可复现，
    宁可漏报也不能因为换了个说法就给出不同结论。
    """
    found: list[str] = []
    for entry in statements:
        allowed = permitted.get(entry["speaker"], set())
        for identifier in sorted(set(permitted.get("__all__", set())) - set(allowed)):
            if identifier and identifier in entry["text"]:
                found.append(f"{entry['speaker']}:{identifier}")
    return tuple(dict.fromkeys(found))


def score_play(
    *,
    required=(),
    engine_spoken=(),
    ai_spoken=(),
    calls=(),
    statements=(),
    permitted=None,
) -> PlayQualityReport:
    """对一局对局的记录打分。

    所有入参都是普通的字典序列，因此既可以来自真实对局的回放，也可以来自
    固定的离线样本；两种来源得到的成绩单可以直接比较。
    """
    statements = list(statements)
    return PlayQualityReport(
        required_speech=_score_required_speech(list(required), list(engine_spoken), list(ai_spoken)),
        repeated_calls=_count_repeated_calls(list(calls)),
        duplicate_statements=_count_duplicate_statements(statements),
        out_of_scope=_find_out_of_scope(statements, permitted or {}),
        statements=len(statements),
    )

#!/usr/bin/env python3
"""对一局已存在的对局打质量分（迭代方案第 12 条）。

用法::

    backend/.venv/bin/python backend/scripts/play_quality_report.py <play_id>

只读取该对局已经产生的记录做统计，不调用任何模型、不产生费用、不修改任何数据。
输出是一张可直接对比的成绩单：改动前后各跑一次，就能看出这次改动让情况
变好了还是变坏了，而不是只留下"感觉好一点"的印象。

不带参数时用内置的合成样本演示输出格式，同样不连数据库。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from src.fusion.play_quality import score_play  # noqa: E402


def demo() -> int:
    """用合成记录演示成绩单，不连数据库、不涉及任何真实剧本内容。"""
    report = score_play(
        required=[
            {'collection': 'memory', 'material_id': 'memory-c', 'owner_character_id': 'c'},
            {'collection': 'knowledge', 'material_id': 'initial-b', 'owner_character_id': 'b'},
        ],
        ai_spoken=[{'collection': 'knowledge', 'material_id': 'initial-b', 'owner': 'b'}],
        engine_spoken=[{'collection': 'memory', 'material_id': 'memory-c', 'owner': 'c'}],
        calls=[
            {'phase_id': 'investigate-one', 'caller': 'b', 'callee': 'c'},
            {'phase_id': 'investigate-one', 'caller': 'c', 'callee': 'b'},
        ],
        statements=[
            {'speaker': 'b', 'text': '我在桥边看到一只纸鹤。', 'sequence': 1},
            {'speaker': 'b', 'text': '我在桥边看到一只纸鹤', 'sequence': 2},
            {'speaker': 'c', 'text': '我什么都没看见。', 'sequence': 3},
        ],
        permitted={'__all__': {'evidence-secret'}, 'b': set(), 'c': set()},
    )
    print("（合成样本演示，未连接数据库）")
    print(report.render())
    print()
    print("传入 play_id 可对真实对局打分：play_quality_report.py <play_id>")
    return 0


def report_for(play_id: str) -> int:
    from src.core.environment import load_project_environment

    load_project_environment()

    from src.db.session import db_manager, get_db_session, init_database
    from src.fusion.package_play import PackagePlayService

    init_database()
    db = next(get_db_session())
    try:
        service = PackagePlayService(db)
        row = service._row(play_id, None)  # 只读回放，不写任何事件
        package, binding = service._resolve(row)
        state = service._replay(row, package, binding)

        engine = state.engine
        catalogue = service._guided_catalog(binding)
        required = list(catalogue._retells.values()) if catalogue else []
        engine_spoken = [
            {'collection': e['collection'], 'material_id': e['material_id'], 'owner': e['owner']}
            for e in state.scripted_retells
        ]
        ai_spoken = [
            {'collection': 'memory', 'material_id': a['memory_id'], 'owner': a['character_id']}
            for a in state.retelling_attempts if a.get('result_status') == 'OK'
        ]
        statements = [
            {'speaker': e['speaker'], 'text': e['text'], 'sequence': e['sequence']}
            for e in state.discussion
        ]
        permitted = {'__all__': set()}
        for character in engine._characters:
            ids = {m['id'] for m in engine.proposal_context(character)['materials']}
            permitted[character] = ids
            permitted['__all__'] |= ids

        print(score_play(
            required=required, engine_spoken=engine_spoken, ai_spoken=ai_spoken,
            statements=statements, permitted=permitted,
        ).render())
        return 0
    finally:
        db.close()
        db_manager.close()


def main() -> int:
    if len(sys.argv) == 1:
        return demo()
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return report_for(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())

"""对局质量评分的行为约定（迭代方案第 12 条）。

评分本身不判断"好不好玩"，它只保证同一份记录每次得到同样的结论，
因此可以用来回答"这次改动让情况变好了还是变坏了"。
"""
from __future__ import annotations

from src.fusion.play_quality import score_play


def _required(*ids):
    return [{'collection': 'memory', 'material_id': i, 'owner_character_id': 'b'} for i in ids]


def _spoken(*ids):
    return [{'collection': 'memory', 'material_id': i, 'owner': 'b'} for i in ids]


def _say(speaker, text, sequence=0):
    return {'speaker': speaker, 'text': text, 'sequence': sequence}


# --------------------------------------------------------------------------
# 必讲要点
# --------------------------------------------------------------------------

def test_a_fully_covered_play_scores_clean():
    report = score_play(required=_required('m1', 'm2'), ai_spoken=_spoken('m1', 'm2'))
    assert report.required_speech.coverage == 1.0
    assert report.required_speech.missing == ()
    assert report.clean


def test_missing_points_are_named_not_just_counted():
    """漏了哪条必须说得出来，否则修的人无从下手。"""
    report = score_play(required=_required('m1', 'm2', 'm3'), ai_spoken=_spoken('m1'))
    assert report.required_speech.covered == 1
    assert report.required_speech.missing == ('m2', 'm3')
    assert not report.clean


def test_engine_substitution_counts_as_covered_but_lowers_the_ai_share():
    """程序代述保证内容不丢，但要如实反映 AI 自己没讲。

    覆盖率和 AI 自述比例必须分开看：只看覆盖率会把"全靠程序兜底"误判成健康。
    """
    report = score_play(
        required=_required('m1', 'm2'),
        ai_spoken=_spoken('m1'),
        engine_spoken=_spoken('m2'),
    )
    assert report.required_speech.coverage == 1.0
    assert report.required_speech.ai_share == 0.5
    assert report.clean, '内容没丢，就不算问题；AI 自述比例单独作为观察指标'


def test_ai_speaking_it_first_is_not_double_counted():
    """AI 讲过之后程序又代述一次，仍算 AI 讲的，不重复计数。"""
    report = score_play(
        required=_required('m1'),
        ai_spoken=_spoken('m1'),
        engine_spoken=_spoken('m1'),
    )
    assert (report.required_speech.spoken_by_ai, report.required_speech.spoken_by_engine) == (1, 0)


def test_a_play_without_required_points_is_not_marked_failing():
    """没有必讲要求的对局不该被判成不及格。"""
    assert score_play().required_speech.coverage == 1.0


# --------------------------------------------------------------------------
# 重复通话
# --------------------------------------------------------------------------

def test_repeated_calls_between_the_same_pair_are_counted():
    calls = [
        {'phase_id': 'p1', 'caller': 'b', 'callee': 'c'},
        {'phase_id': 'p1', 'caller': 'b', 'callee': 'c'},
    ]
    assert score_play(calls=calls).repeated_calls == 1


def test_direction_does_not_make_it_a_different_call():
    """b 打给 c 之后 c 又打给 b，对玩家而言就是这两人又聊了一次。"""
    calls = [
        {'phase_id': 'p1', 'caller': 'b', 'callee': 'c'},
        {'phase_id': 'p1', 'caller': 'c', 'callee': 'b'},
    ]
    assert score_play(calls=calls).repeated_calls == 1


def test_calls_in_different_phases_are_not_repeats():
    calls = [
        {'phase_id': 'p1', 'caller': 'b', 'callee': 'c'},
        {'phase_id': 'p2', 'caller': 'b', 'callee': 'c'},
    ]
    assert score_play(calls=calls).repeated_calls == 0


# --------------------------------------------------------------------------
# 重复发言
# --------------------------------------------------------------------------

def test_near_identical_statements_count_as_duplicates():
    """标点、空格和全角半角的差别不应让重复逃过统计。"""
    statements = [
        _say('b', '我在桥边看到一只纸鹤。', 1),
        _say('b', '我在桥边看到一只纸鹤', 2),
        _say('b', '我在桥边，看到一只纸鹤！', 3),
    ]
    assert score_play(statements=statements).duplicate_statements == 2


def test_different_speakers_saying_the_same_thing_is_not_a_duplicate():
    """两个人说同一句话是剧情，不是 AI 复读。"""
    statements = [_say('b', '我什么都没看见', 1), _say('c', '我什么都没看见', 2)]
    assert score_play(statements=statements).duplicate_statements == 0


def test_blank_statements_are_ignored():
    statements = [_say('b', '   ', 1), _say('b', '。。。', 2)]
    assert score_play(statements=statements).duplicate_statements == 0


# --------------------------------------------------------------------------
# 越权提及
# --------------------------------------------------------------------------

def test_mentioning_a_material_the_speaker_cannot_know_is_reported():
    """说出自己无权知道的材料，是最严重的一类问题，必须点名。"""
    report = score_play(
        statements=[_say('b', '钥匙在 evidence-secret 里', 1)],
        permitted={'__all__': {'evidence-secret', 'evidence-open'}, 'b': {'evidence-open'}},
    )
    assert report.out_of_scope == ('b:evidence-secret',)
    assert not report.clean


def test_mentioning_permitted_material_is_fine():
    report = score_play(
        statements=[_say('b', '我看过 evidence-open', 1)],
        permitted={'__all__': {'evidence-secret', 'evidence-open'}, 'b': {'evidence-open'}},
    )
    assert report.out_of_scope == ()
    assert report.clean


def test_the_same_violation_is_reported_once():
    report = score_play(
        statements=[_say('b', 'evidence-secret', 1), _say('b', '还是 evidence-secret', 2)],
        permitted={'__all__': {'evidence-secret'}, 'b': set()},
    )
    assert report.out_of_scope == ('b:evidence-secret',)


# --------------------------------------------------------------------------
# 成绩单
# --------------------------------------------------------------------------

def test_the_report_names_every_problem_it_found():
    report = score_play(
        required=_required('m1', 'm2'),
        ai_spoken=_spoken('m1'),
        calls=[{'phase_id': 'p1', 'caller': 'b', 'callee': 'c'}] * 2,
        statements=[_say('b', '同一句话', 1), _say('b', '同一句话', 2)],
        permitted={'__all__': {'m9'}, 'b': set()},
    )
    text = report.render()
    assert 'm2' in text, '漏讲的要点要点名'
    assert '重复通话            1' in text
    assert '近似重复发言        1' in text
    assert '结论：存在上列问题' in text


def test_a_clean_report_says_so_plainly():
    text = score_play(required=_required('m1'), ai_spoken=_spoken('m1')).render()
    assert '结论：无已知问题' in text


def test_scoring_the_same_record_twice_gives_the_same_answer():
    """同一份记录必须每次得到相同结论，否则无法用来比较改动前后。"""
    inputs = dict(
        required=_required('m1', 'm2'),
        ai_spoken=_spoken('m1'),
        statements=[_say('b', '一句话', 1), _say('b', '一句话', 2)],
    )
    assert score_play(**inputs).render() == score_play(**inputs).render()

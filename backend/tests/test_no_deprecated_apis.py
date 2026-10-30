"""不再引入已弃用的框架写法（迭代方案第 11 条）。

清理前全项目有 363 条弃用警告。它们现在都还能跑，但下一次 Pydantic 或
SQLAlchemy 大版本升级会一次性全部失效——那时是在压力下被迫修改，
比现在从容替换贵得多。清零之后需要一条检查守住，否则会慢慢长回来。

检查针对源码文本而不是运行时警告：有些写法只在特定代码路径被执行时才告警，
测试跑不到的地方就看不见。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# 每条规则：正则、替代写法、为什么要换。
FORBIDDEN = [
    (
        re.compile(r"\bdatetime\.utcnow\s*\("),
        "datetime.now(timezone.utc)",
        "utcnow() 返回不带时区的时间，Python 已弃用；混用会造成跨时区比较错误。",
    ),
    (
        re.compile(r"from sqlalchemy\.ext\.declarative import declarative_base"),
        "from sqlalchemy.orm import declarative_base",
        "SQLAlchemy 2.0 起 declarative_base 已迁移到 sqlalchemy.orm。",
    ),
    (
        re.compile(r"^\s*from pydantic import .*\bvalidator\b(?!\w)", re.M),
        "field_validator",
        "Pydantic V1 风格的 validator 将在 V3 移除。",
    ),
    (
        re.compile(r"@validator\("),
        "@field_validator(（并加 @classmethod）",
        "Pydantic V1 风格的 validator 将在 V3 移除。",
    ),
    (
        re.compile(r"\bmin_items\s*="),
        "min_length=",
        "Pydantic V2 用 min_length 取代 min_items。",
    ),
    (
        re.compile(r"\.from_orm\("),
        "model_validate(",
        "Pydantic V2 用 model_validate 取代 from_orm。",
    ),
    (
        re.compile(r"^\s*class Config:\s*$", re.M),
        "model_config = ConfigDict(...)",
        "Pydantic V2 用 ConfigDict 取代类式 Config。",
    ),
]


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_source_file_uses_no_deprecated_api(path: Path):
    text = path.read_text(encoding="utf-8")
    problems = [
        f"{pattern.pattern} → 请改用 {replacement}（{reason}）"
        for pattern, replacement, reason in FORBIDDEN
        if pattern.search(text)
    ]
    assert not problems, (
        f"{path.relative_to(SRC)} 使用了已弃用的写法：\n  " + "\n  ".join(problems)
    )

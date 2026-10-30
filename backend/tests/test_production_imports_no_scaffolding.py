"""正式运行路径不得依赖测试脚手架（迭代方案第 13 条）。

``src/fusion`` 下的 ``*_smoke.py`` 与 ``*_probe.py`` 是人工验证真实 API 用的脚本，
约 2900 行，却和业务代码放在同一个包里，会跟着一起打包进正式镜像。此前它们
确实被生产代码引用（配置加载写在冒烟脚本里），现在配置已抽到
``src/fusion/provider_config.py``，因此可以从镜像中排除。

这条检查保证这个前提持续成立：一旦有人从正式运行路径引回脚手架，
镜像里没有这些文件，线上会直接 ImportError 起不来——那种失败发生在部署时，
远比在这里失败昂贵。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

FUSION = Path(__file__).resolve().parents[1] / "src" / "fusion"
SRC = Path(__file__).resolve().parents[1] / "src"


def _scaffolding_module_names() -> set[str]:
    return {path.stem for path in [*FUSION.glob("*_smoke.py"), *FUSION.glob("*_probe.py")]}


def _production_modules() -> list[Path]:
    scaffolding = _scaffolding_module_names()
    return [p for p in SRC.rglob("*.py") if p.stem not in scaffolding]


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.rsplit(".", 1)[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.rsplit(".", 1)[-1])
    return names


def test_scaffolding_modules_are_actually_present():
    """前提检查：确实存在这些脚手架，否则本文件的其余用例会变成空转。"""
    assert _scaffolding_module_names(), "没有找到任何脚手架模块，本检查可能已失效"


@pytest.mark.parametrize(
    "module", _production_modules(), ids=lambda p: str(p.relative_to(SRC))
)
def test_no_production_module_imports_scaffolding(module: Path):
    """正式模块不得 import 任何 ``*_smoke`` / ``*_probe``。

    需要其中某个功能时，把它抽到一个正式命名的模块里再引用——
    provider_config 就是这样从 provider_smoke 里分出来的。
    """
    offending = _imported_names(module) & _scaffolding_module_names()
    assert not offending, (
        f"{module.relative_to(SRC)} 引用了测试脚手架 {sorted(offending)}。\n"
        "这些模块已被 .dockerignore 排除出正式镜像，线上不存在，"
        "引用它们会导致部署后启动失败。请把需要的部分抽成正式模块。"
    )


def test_dockerignore_still_excludes_scaffolding():
    """排除规则必须留在 .dockerignore 里，否则上面的检查就失去意义。"""
    text = (Path(__file__).resolve().parents[1] / ".dockerignore").read_text(encoding="utf-8")
    assert "src/fusion/*_smoke.py" in text
    assert "src/fusion/*_probe.py" in text

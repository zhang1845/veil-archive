"""Small invented case for isolated generation integration tests."""
from src.fusion.creative_generation import CreativeDraft


def story():
    return CreativeDraft(
        introduction="山间小站的登记簿消失了。晚班结束前只有三个人留在站内，雨一直下到午夜。你们需要根据交接记录和现场物证核对事情经过。所有人物与事件均为测试虚构。",
        characters=[dict(name=name, profile=f"{name}是小站当晚的值班人员，负责各自的交接事务。",
            background=f"{name}在二十点之前来到小站。你亲眼看见走廊的灯在二十一点熄灭，随后在休息室整理纸质交接材料，直到听到站长询问登记簿的去向。",
            shareable_clue=f"{name}记得二十一点十分门边出现一双带泥的鞋，随后有人把蓝色文件袋带进了仓库。",
            secret=f"{name}保守的秘密标记为 PRIVATE-{i}，你曾偷偷翻过一份旧账簿，不愿让别人知道。") for i, name in enumerate(["林岚", "陈渡", "周禾"])],
        evidence=[dict(text="走廊的时钟停在二十一点零五分，背面的电池盖上有新鲜泥点，站内其他时钟工作正常。"),
                  dict(text="仓库的蓝色文件袋中夹着一角登记簿纸张，袋底沾有仅在站后小路出现的红色黏土。"),
                  dict(text="陈渡的交接单上记载二十一点零八分曾去过站后小路，林岚和周禾的签字时间均早于熄灯。")],
        truth="FINAL-TRUTH：陈渡在熄灯后通过站后小路进入仓库，把登记簿藏入蓝色文件袋。他想掩盖自己漏记的一批物资。泥点、纸角和交接时间三者可以相互印证。林岚和周禾都只翻过旧账簿，与这次藏匿无关。此案件只用于合成软件测试。",
        ending="按已公开证据核对三人的行踪，再揭晓登记簿被藏匿的经过。对照物证说明推理中哪些说法缺乏支持，完成复盘。")


def compiled_package(context, draft):
    by_path = {s["relative_path"]: s for s in context["sources"]}
    def ref(path, anchor):
        return [{"source_id": by_path[path]["id"], "anchor": anchor}]
    rule = lambda p: {"phase_id": p, "required_public_evidence_ids": []}
    knowledge, characters = [], []
    for i, char in enumerate(draft.characters):
        identifier = f"c{i}"
        path = f"role-{i + 1}.md"
        characters.append({"id": identifier, "name": char.name, "sources": ref(path, "角色")})
        for suffix, text, disclosure, phase, anchor in [
            ("profile", char.profile, "PUBLIC", "reading", "公开介绍"),
            ("background", char.background, "KEEP_PRIVATE", "reading", "本人阅读阶段私本"),
            ("clue", char.shareable_clue, "MAY_SHARE", "investigation", "调查阶段可转述线索"),
            ("secret", char.secret, "KEEP_PRIVATE", "reading", "永久保密")]:
            knowledge.append({"id": f"{identifier}-{suffix}", "text": text, "kind": "FACT",
                "visibility": "PUBLIC" if disclosure == "PUBLIC" else "CHARACTER_PRIVATE",
                "character_id": None if disclosure == "PUBLIC" else identifier, "release": rule(phase),
                "disclosure": disclosure, "sources": ref(path, anchor)})
    return {"schema_version": "script-package/1.1", **{k: context[k] for k in ("script_key", "content_version", "title", "player_count", "sources")},
        "initial_phase_id": "reading", "introduction": {"text": draft.introduction, "sources": ref("overview.md", "公开开场")},
        "characters": characters, "phases": [{"id": p, "title": title, "next_phase_id": nxt, "sources": ref("overview.md", "阶段规则")} for p, title, nxt in
            [("reading", "阅读", "investigation"), ("investigation", "调查", "finale"), ("finale", "终局", None)]],
        "knowledge": knowledge,
        "evidence": [{"id": f"e{i}", "text": item.text, "visibility": "PUBLIC", "character_id": None, "release": rule("investigation"), "disclosure": "PUBLIC", "sources": ref("evidence.md", f"调查阶段公开物证{i + 1}")} for i, item in enumerate(draft.evidence)],
        "truth": [{"id": "truth", "text": draft.truth, "visibility": "SYSTEM_TRUTH", "sources": ref("truth.md", "仅结算揭晓的系统真相")}],
        "settlement": {"phase_id": "finale", "truth_ids": ["truth"], "instructions": {"text": draft.ending, "sources": ref("truth.md", "终局结算说明")}}}

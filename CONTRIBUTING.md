# 贡献指南

欢迎为「雾幕档案」贡献代码。本文档说明如何搭建环境、运行测试、提交改动。

## 这是什么项目

面向手机浏览器的单真人 AI 剧本杀：玩家选择一个角色，其余角色由 AI 演绎。
**规则引擎是阶段、搜证、证据可见性和投票的唯一权威；云模型只生成角色候选发言，
不能直接修改游戏状态。**

技术栈：FastAPI + PostgreSQL。Python 3.13。（前端界面当前已移除，待重建，保留 Next.js）

## 环境搭建

先读 [从零搭建本机环境](docs/development/LOCAL_SETUP_FROM_SCRATCH.md)，它不需要 Docker、
也不需要管理员密码。已有 `.env` 时不要覆盖。

装好之后启动：

```bash
backend/.venv/bin/python scripts/local_postgres.py start
backend/.venv/bin/python scripts/local_redis.py start
cd backend && ./.venv/bin/python -B main.py        # 后端 127.0.0.1:8010
```

后端 API 地址 <http://127.0.0.1:8010>。前端界面已移除待重建。

## 运行测试

改动提交前，请确认以下检查全部通过。

### 后端

```bash
# 开发预检（离线，不读 .env、不连数据库）
backend/.venv/bin/python scripts/dev_preflight.py

# 预检自测
backend/.venv/bin/python -m unittest discover -s scripts -p 'test_dev_preflight.py'

# 安全/规则测试（离线，不连数据库、不产生费用）
backend/.venv/bin/python backend/scripts/test_fusion_security.py

# 全量测试（需要本地 PostgreSQL）
backend/.venv/bin/python -m pytest backend/tests -q
```

### 前端

> 前端界面当前已移除，待重建（保留 Next.js 技术栈）。重建后再补充前端测试与类型检查命令。

## 代码规范

- **Python**：3.13.x，类型标注遵循 `pyrightconfig.json`（不要求 pyright 零错误，
  但新代码应避免引入新的明显类型错误）。
- **不要用粗体（font-weight 700+）**：设计规范上限 590，详见 `DESIGN.md`。
- **颜色用语义 token**（`bg-ink`、`text-paper`、`text-brass` 等），不要硬编码 hex。

## 安全边界（改动时务必遵守）

这是本项目最重要的一条约束，改代码前请先理解：

1. **规则引擎是唯一权威**：LLM 只能生成受限的「角色发言」候选，不能改游戏状态、
   不能读私本、不能越权访问其他角色材料。
2. **发布必须经过人审**：剧本从候选到发布，必须绑定来源 + 人工审核 + 模型审核，
   缺一不可。**不要为「省事」绕过发布门禁**。
3. **权限按角色隔离**：材料按 `script/version/session/character/phase` 过滤，
   先授权后检索。
4. **敏感文件不进仓库**：`.env`、密钥、商业剧本正文、OCR、游戏存档都不得提交。

## 提交规范

使用 conventional commits 风格：

```
feat: 新功能
fix: 修复 bug
refactor: 重构（不改变行为）
docs: 文档
test: 测试
chore: 杂项
```

示例：`fix: 修复游戏历史接口的越权访问`

## 目录结构

```
backend/            # FastAPI 后端
  src/fusion/       # 单真人玩法核心（规则引擎、材料问答、调查、终局）
  src/schemas/      # 数据契约
  src/db/           # ORM 与迁移
  tests/            # 测试
docs/               # 开发记录与契约
```

## 提问与反馈

开 issue 前请先读 `readme.md` 顶部的「现在是什么状态」，确认问题是否已记录。

# CloudBase 免费版部署

这套入口为当前 CloudBase PostgreSQL 免费环境适配，保留雾幕档案前端、独立玩家身份、
确定性游玩和管理员候选／人工复核／发布门禁。它与原有 Compose 部署并存，
后端入口使用 `src.cloudbase.app:create_app`，不会启动原 PostgreSQL、Redis 或本地文件 worker。

当前免费方案使用 CloudBase Run、共享 PostgreSQL 的 HTTPS 网关、私有 PG Storage 和静态托管。
不需要 VPC、原生 PostgreSQL TCP 地址、独立 Redis、CFS 或常驻实例。
免费套餐仍受环境有效期和资源点配额限制；运行、数据库、存储和流量均需按该环境实际额度核对，
这里不承诺永久免费或无限访问。不要为此方案自动升级套餐、开启超额付费或模型付费。

## 1. 数据库与存储

共享 PostgreSQL 使用服务端 HTTPS RPC，不能把它的网关地址填进原 SQLAlchemy 的 `DB_HOST`。
SQLAlchemy／内存 SQLite 在这套入口中用于执行原确定性领域逻辑；它们的内存数据库不是持久存档。
最终记录通过 PostgreSQL 的版本检查和幂等回执原子提交，容器本地文件仅作可删除的临时缓存。
访问方式见 [CloudBase PG 连接说明](https://docs.cloudbase.net/database/postgresql/connecting-to-postgresql)
与 [RPC 权限说明](https://docs.cloudbase.net/database/postgresql/rpc)。

在**目标环境的 SQL 编辑器／迁移入口**按下面顺序安装完整文件，每次确认事务成功后再执行下一份：

| 顺序 | 文件 | 用途 |
| --- | --- | --- |
| 1 | `001_aggregate_store.sql` | 聚合存档、版本 CAS、原结果幂等回执、函数内服务角色校验 |
| 2 | `002_auth.sql` | 本应用独立账号、访问／刷新令牌哈希与数据库限流 |
| 3 | `003_jobs.sql` | 任务租约、执行栅栏、用量预占、一次 dispatch 与结果账本 |
| 4 | `004_guarded_cas.sql` | 同一事务锁定存档及发布依据，防止审批变化与新开局竞争 |
| 5 | `005_job_cancel.sql` | 管理员安全取消、版本检查与取消回执 |

这些脚本操作本应用的 `public.veil_archive_*` 对象。不要在共享环境运行旧部署的
`create_all`、清库或全库恢复脚本，也不要修改其他业务表。`005` 只扩展本应用任务表的终态约束。
表启用 RLS，匿名、玩家及服务角色都没有这些私有表的直接读写权限；窄 RPC 在函数内部再次校验
`service_role`，权限边界不能只依赖 `GRANT EXECUTE`。

`003` 对新环境默认设置 `paid_execution_enabled=false`，重新执行时不会覆盖已有开关。
部署时应核对它仍为 `false`：

```sql
SELECT paid_execution_enabled
FROM public.veil_archive_job_settings
WHERE singleton;
```

本免费方案不打开该开关。取消任务也不会删除、重发或退款未知调用；活动租约、未核实预占、
`IN_FLIGHT` 和 `UNKNOWN` 会阻止取消，已有费用记录保持。

来源、核验报告等文件需要**本应用专用的私有 PG Storage bucket**。
通过服务端 `PrivateBlobStore` 上传并核对内容哈希，来源注册和管理员初始化仅供受控初始化程序使用，
没有公开 bootstrap 路由。不要把已有共享 bucket 改成公开，也不要把来源文件放进前端 `public/`。
未配置来源存储时，候选读写和发布检查仍可用，来源核验会明确报错。

## 2. 后端 Run 服务

默认选择 **0.5 核 CPU、1 GiB 内存、最小实例数 0、最大实例数 1**，端口 **8080**。
空闲时允许缩容至 0，首次请求可能冷启动；不启动常驻 worker。
对浏览器提供 HTTPS 服务入口，应用自己的玩家／管理员身份校验继续生效。
不要要求浏览器携带 CloudBase 服务端 API Key。

在仓库根生成最小上传包，选择一个尚不存在的输出文件名：

```sh
python3 deploy/cloudbase/package_backend.py /tmp/veil-archive-cloudbase-backend.zip
```

ZIP 根目录包含 `Dockerfile`、`pyproject.toml`、`uv.lock` 和 `src/`。
使用这个 ZIP 的根目录作为构建上下文；不要把整个资料目录或本机环境上传。
脚本不会打包 `.env`、数据库、依赖目录或私有运行数据。
容器以非 root 用户运行，启动命令已写在 [Dockerfile](Dockerfile) 中。

仅在服务端配置以下变量：

| 变量 | 内容 |
| --- | --- |
| `CLOUDBASE_ENV_ID` | 当前 PG 模式环境 ID |
| `CLOUDBASE_APIKEY` | CloudBase Run 安全绑定注入的服务端 API Key 别名 |
| `CLOUDBASE_API_KEY` | 不使用上述绑定时的显式服务端变量，优先级高于 `CLOUDBASE_APIKEY` |
| `CORS_ORIGINS` | 前端 HTTPS **origin**，多个用逗号分隔；不带路径、末尾斜杠或通配符 |
| `CLOUDBASE_PRIVATE_BUCKET` | 本应用私有 PG Storage bucket 名；来源核验／正式来源读取需要 |

例如站点位于 `https://static.example.invalid/veil-archive/`，CORS 填写
`https://static.example.invalid`，不填 `/veil-archive/`。
两个 Key 变量选择一种维护；换用新的安全绑定时应移除过期的显式值，防止旧值继续覆盖绑定。
Key 不进入前端、Git、ZIP、截图或日志。

**本次选择的服务端 Key 有效期为 30 天**，以控制台显示的实际到期时间为准。
到期前创建并绑定替换 Key，更新后端版本，验证 `/health`、玩家登录和私有数据读取，
确认成功后再停用旧 Key。它与玩家访问／刷新令牌是不同凭据，用户重新登录不能修复后端 Key 过期。
这里不提供定时轮换程序或自动续期承诺。

## 3. 独立前端子目录

为了保护共享环境中的其他网站，静态站点固定部署在 **`/veil-archive/`**，
不要覆盖托管根目录 `index.html` 或清理整个静态站点。
在 `frontend/` 安装锁定依赖后，用实际 Run HTTPS 根地址构建：

```sh
npm ci
CLOUDBASE_STATIC_EXPORT=true NEXT_PUBLIC_API_URL=https://实际后端域名 npm run build
```

若本机借用仓库外 `node_modules` 软链，Turbopack 可能拒绝跨根目录链接，可用
`npm run build -- --webpack` 构建同一静态产物；部署包不包含这个软链。

静态模式默认 `basePath=/veil-archive`、`trailingSlash=true`、图片不依赖 Next 图片服务。
产物为 `frontend/out/`，上传其**内容**到托管的 `veil-archive/` 子目录。
首页、剧本库、游玩、记录、教程、管理端和登录页均输出目录 `index.html`，例如
`veil-archive/scripts/index.html`；脚本、图标和背景图片也带同一路径前缀。
如果控制台只能向托管根目录解压 ZIP，ZIP 必须包含 `veil-archive/` 外层目录。
具体打包说明见 [前端说明](../../frontend/CLOUDBASE_STATIC_EXPORT.md)。

`NEXT_PUBLIC_API_URL` 是公开地址，不含 `/api` 后缀，不是 Key。
更换后端地址后必须重新构建；修改 Run 环境变量不会更新已经生成的前端文件。
不要上传使用 `cloudbase-api.invalid` 等占位地址构建的验证产物。
普通部署不设置 `CLOUDBASE_STATIC_EXPORT=true`，仍保持原 standalone 和空 `basePath`。

## 4. 当前真实能力与限制

- 公开试玩使用独立 `preview_manifest` 白名单，界面显示“公开试玩／原创测试剧本／模型审核未完成／AI 对话关闭”。
  它不属于正式发布，没有伪造模型审核、人审确认或正式 release 记录，也不沿用仅限本机的 `LOCAL_TRIAL` 放行规则。
- 玩家可独立选角、阅读、搜证、公开发言、推进阶段、查看结局和恢复自己的记录。实时 AI 对话关闭。
- 管理员候选导入、确定性校验、来源复核、人工报告、问题处置与确认／发布接口复用原领域服务。
  正式发布仍要求真实且完整的模型审核证据、人工审核及当前有效确认；缺任一条件会拒绝发布。
- 云版 AI 原创、编译、自动改稿和模型复审执行适配器尚未接通，当前也未配置模型。
  创建入口明确返回 `CLOUDBASE_MODEL_DISABLED`，不会生成假排队任务。
  现有 `003` 任务事务基础不等于创作流程可执行；仅补一个模型 Key 不能自动接通该流程。
- 正式当前／历史快照读取和依赖校验代码不代表已经完成真实模型审核、正式发布与正式整局的线上验收。
  本次上线不得用公开试玩结果替代这些验收。
- 当前每个玩家工作区最多 20 局、3000 条事件、900000 字节；管理工作区也受 900000 字节上限约束。
  达到限制会拒绝新写入，不静默删除历史或把容器磁盘当作持久存储。

初始化管理员、原创试玩清单或来源应通过受控服务端程序完成，保留原内容和哈希，
不把这些初始化方法挂为网站接口。`/health` 依赖试玩清单正确安装；清单缺失、Key 失效或数据库不可用时
返回 503，不能把“容器已启动”当作功能已可用。

部署验收应覆盖实际 HTTPS 首页与带前缀的资源、独立访客隔离、选角到结局、刷新恢复、
管理员权限、真实候选保存以及未满足审核条件时发布被拒绝。
隔离单测和本机 PostgreSQL 并发测试不替代 CloudBase 网关、Key、私有桶和浏览器跨域的实际验证。

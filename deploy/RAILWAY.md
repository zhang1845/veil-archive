# Railway 五服务部署

本项目保留完整Next.js / FastAPI / PostgreSQL / Redis架构，使用一个Caddy服务提供同源HTTPS入口。以下配置适用于单个项目、同一环境、单backend副本。不要开启跨区域副本：当前文件账本和worker租约要求共享同一backend持久卷。

Railway不直接运行Compose文件，每个服务单独创建。使用平台当前的服务设置界面，不新增已被官方弃用的legacy `railway.json`。官方说明：[Docker Compose映射](https://railway.com/guides/docker-compose)、[Dockerfile路径](https://docs.railway.com/builds/dockerfiles)、[私网](https://docs.railway.com/networking/private-networking)、[配置文件弃用说明](https://docs.railway.com/config-as-code/reference)。

## 服务与构建

先创建PostgreSQL和Redis，再配置backend、frontend，最后创建proxy。所有服务使用同一区域和环境。只有proxy生成公网域名，数据库、Redis、backend与frontend不开放公网域名或TCP代理。

| 服务名 | 来源 / Root Directory | 构建 / 运行 | 健康检查 | 持久卷 |
| --- | --- | --- | --- | --- |
| Postgres | Railway PostgreSQL | 平台数据库模板 | 平台默认 | 平台数据库卷 |
| Redis | Railway Redis | 平台缓存模板 | 平台默认 | 平台缓存卷 |
| backend | GitHub仓库，`/backend` | 自动识别Dockerfile；不要覆盖默认启动命令 | `/health`，超时300秒 | `/private-data` |
| frontend | 同一仓库，`/frontend` | 自动识别Dockerfile；默认 `node server.js` | `/` | 无 |
| proxy | 同一仓库，`/` | `RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile.proxy` | `/health` | 无 |

网站域名指向proxy的8080端口。Railway边缘提供HTTPS，Caddy在容器内监听HTTP `:8080`。Caddy自动处理WebSocket升级，`/api/*`、`/health`、`/static/*`去backend，其余去frontend，API目录入口返回404。

前端构建参数 `NEXT_PUBLIC_API_URL` 保持 `/`。该值在镜像构建时固化；改运行时变量不能修复已经编译进浏览器的错误API地址。部署过程中不能设置为 `http://127.0.0.1:8010`。

## backend变量

下面的引用名称必须与项目内实际数据库服务名一致。不要复制数据库公网URL，使用平台私网引用变量。

```dotenv
ENV=production
HOST=::
PORT=8010
FILE_STORAGE=dir
CONTAINER_STORAGE_ROOT=/private-data
RUN_DATABASE_MIGRATIONS=true
DB_HOST=${{Postgres.PGHOST}}
DB_PORT=${{Postgres.PGPORT}}
DB_NAME=${{Postgres.PGDATABASE}}
DB_USER=${{Postgres.PGUSER}}
DB_PASSWORD=${{Postgres.PGPASSWORD}}
DB_POOL_SIZE=5
REDIS_URL=${{Redis.REDIS_URL}}
ACCESS_TOKEN_EXPIRE_MINUTES=120
REFRESH_TOKEN_EXPIRE_MINUTES=43200
ALLOW_ANONYMOUS_ACCESS=false
ALLOW_LEGACY_REGISTRATION=false
SMS_PROVIDER=disabled
INVITE_CODES=
CORS_ORIGINS=
ENABLE_LOCAL_TRIALS=false
ENABLE_LEGACY_ADMIN=false
ENABLE_MEDIA_FEATURES=false
ENABLE_PUBLIC_API_DOCS=false
ENABLE_PAID_MODEL_CALLS=false
ENABLE_CREATIVE_WORKER=false
ENABLE_DOUBAO_ASR=false
ENABLE_QWEN_ASR=false
ENABLE_QWEN_REALTIME_ASR=false
```

另外在Railway变量面板生成并保存专用的 `SECRET_KEY`，至少32个随机字符，不使用模板值、不上传到GitHub、不写到聊天或日志。`CORS_ORIGINS` 同源时可空；若明确采用其他网页源，填入实际允许的HTTPS源，不能用示例域名。

`CONTAINER_STORAGE_ROOT`启用容器专用入口，默认创建私有的source-bundles、creative-jobs、speech-input、uploads和static子目录，并将 `.data` 和 `static` 映射到同一卷内。只允许空镜像目录或已知placeholder；发现未知文件、错误符号链或卷外私有路径会拒绝启动，不能删除数据来绕过。

不要在启动命令中重复添加Alembic。`RUN_DATABASE_MIGRATIONS=true` 已使入口先做生产配置检查，再执行迁移，最后启动API。迁移和数据库故障会使部署失败，应检查服务目标与日志，不通过开发模式绕过。

## frontend变量

```dotenv
HOSTNAME=::
PORT=8009
NEXT_TELEMETRY_DISABLED=1
```

## proxy变量

```dotenv
PORT=8080
SITE_ADDRESS=:8080
BACKEND_UPSTREAM=backend.railway.internal:8010
FRONTEND_UPSTREAM=frontend.railway.internal:8009
RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile.proxy
```

若服务名不同，替换为实际私网域名，使用HTTP私网连接。新环境支持私网IPv4与IPv6，旧环境可能只有IPv6，因此应用监听 `::`；Compose仍保持其独立的IPv4容器设置。

## 账号初始化与内容

数据库迁移和backend健康检查成功后，通过已登录且获授权的平台终端运行 `python scripts/bootstrap_account.py` 创建独立管理员和玩家，使用隐藏输入或安全stdin。脚本不能创建schema、重置现有密码或自动把已有玩家升为管理员。具体参数见 [生产部署](README.md)。若平台终端不支持隐藏输入，应使用安全的stdin执行通道，不能把密码放进命令行、仓库、截图或部署日志。

上线不自动迁入本机存档和来源；本机的未审LOCAL_TRIAL在生产被禁用。新剧本仍需完整模型审核、人工审核、显式确认和发布。不要把生产改成development来让试用剧本出现。

## 可选同卷创作worker

五服务方案不创建第六个独立worker，避免来源与创作账本分落两个不能共享的卷。需要真实AI创作时，管理员先配置合法的百炼密钥、费率与预算，再明确同时启用：

```dotenv
ENABLE_PAID_MODEL_CALLS=true
ENABLE_CREATIVE_WORKER=true
```

容器入口在同一backend服务内管理API和worker，任一退出会收尾整个服务，不重新发送失败或未知模型调用。默认两项均关闭，不因上线自动启用。保持一个backend副本；不允许同时再用worker命令重复启动第二个worker。

## 费用、重启与验收

使用平台适用的免费额度或明确批准的费用上限；不自动升级套餐或绑定支付卡。实际费用以[Railway价格与用量](https://railway.com/pricing)为准，五服务不代表长期免费。试用/免费环境使用On Failure、最多10次重启；健康检查仅用于新部署切换，不能代替持续监控。

部署完成后检查首页、教程、玩家登录、管理员登录、未授权后台拒绝、`/health`、API文档404，以及backend重启后的账号和文件持久化。每个玩家使用独立账号。使用经过正式发布的内容验收选角、搜证、结算和刷新恢复；没有正式剧本时，应如实显示空库，不能注入演示材料冒充正式审核。

本机未实际构建或运行Docker镜像时，文档和离线测试只能说明配置已准备，云端构建、数据库迁移、域名和真实模型仍需要分别验收。

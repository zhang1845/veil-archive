# 雾幕档案生产部署

本仓库包含 Next.js 前端、FastAPI 后端、PostgreSQL、Redis 和同源 Caddy 入口。网站入口可公开，数据库、缓存、后台服务和私有来源卷只在服务内网使用。首次上线不会自动复制本机账号、游戏存档或未正式审核的试玩剧本。

## Docker Compose

在独立的新部署主机上使用 Docker Engine 与 Compose 插件。不要覆盖开发机已有的 `.env`，不要把本机 `private-data` 整体复制到线上。

1. 在部署目录创建专用的私有 `.env`，设置随机 `DB_PASSWORD`、至少32字符的随机 `SECRET_KEY`，以及 `SITE_ADDRESS`。有域名时使用域名；本机验收可使用 `:80`。`CORS_ORIGINS` 使用实际网站源；同源部署可留空。密钥只保存在服务器或平台的变量配置中。
2. 执行 `docker compose config --quiet` 检查配置，再执行 `docker compose up -d --build`。默认仅启动前端、后端、数据库、Redis 和 Caddy。后端先进行生产安全检查，再执行 Alembic 迁移。
3. 检查 `docker compose ps`，确认网站首页和 `/health` 返回成功。只有 Caddy 对外监听80/443，其他服务未映射公网端口。
4. 在确认是目标生产数据库后，显式创建管理员和独立玩家账号。密码通过终端隐藏输入，已有账号不会被重置或提权。

```sh
docker compose exec backend python scripts/bootstrap_account.py --username owner --email owner@example.com --role admin
docker compose exec backend python scripts/bootstrap_account.py --username player1 --email player1@example.com --role player
```

将示例邮箱替换为实际邮箱。玩家使用 `/player-login`，管理员使用 `/login`。没有接入真实短信供应商时，不开放短信登录或共享访客身份；初始玩家账号由管理员分配。

### 持久化与备份

Compose保持数据库、Redis、后端上传 `.data`、公开静态文件和私有来源为独立命名卷。后端代码位于 `/app/backend`，私有来源位于 `/private-data`；前端和Caddy没有私有卷访问权。

生产数据卷不能通过 `docker compose down -v` 清理。备份数据库同时备份对应版本的来源快照、核验报告和创作账本，并在隔离环境验证恢复。`deploy/backup-postgres.sh` 仅是数据库备份工具，不能代替私有文件卷备份；按主机实际的账号、数据库名、保留策略和权限配置后再使用。

### AI 创作服务

默认不开启真实模型调用或创作worker。管理员明确配置模型凭据、费率和预算，并将 `ENABLE_PAID_MODEL_CALLS=true` 后，才可显式启动可选的创作服务：

```sh
docker compose --profile creative up -d creative-worker
```

该profile和全局付费开关共同约束执行。不会自动批准发布，失败或未知调用不会自动重发。首次上线不要为了出现剧本而开启开发环境、共享guest或本地试玩发布开关。

## 腾讯云

轻量应用服务器/CVM的新主机操作步骤、私有仓库克隆、专用配置、账号和HTTPS见 [腾讯云部署](TENCENT_CLOUD.md)。可选AI创作需显式选择只读配置挂载override，不能把秘密打进镜像。

## Railway

平台五服务、单backend持久卷的配置见 [Railway部署](RAILWAY.md)。不要把Compose文件误当作Railway可直接执行的编排文件，也不要将前端地址编译成开发机的 `127.0.0.1`。

## 验收边界

本机没有安装Docker时，只能进行源码、静态配置、构建和隔离测试，不能声称容器、迁移或云端已经通过。线上验收必须检查生产环境安全自检、数据库迁移、HTTPS、管理员/玩家账号、拒绝访问后台、独立玩家资料边界、重启后的持久化以及正式剧本发布。

本地 `LOCAL_TRIAL` 记录在生产环境不会显示。正式内容需要完成对应版本的真实模型审核、人工审核、确认和发布；上线代码并不代表这些内容步骤已经完成。

本次生产准备验证：后端3061项隔离禁网回归通过；前端53项、ESLint/TypeScript、Webpack生产构建通过；新入口和账号工具Pyright无错误。Compose及AI只读配置挂载做了静态核对，未实际运行腾讯云Docker、迁移或证书。

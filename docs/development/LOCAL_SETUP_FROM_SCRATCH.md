# 从零搭建本机运行环境

更新：2026-09-12。本页记录这台 Mac 上环境实际是怎么装起来的，供重装或换机时复现。
[小白本地启动说明](LOCAL_DEVELOPMENT.md)描述的是装好之后的日常用法，本页补上"装"这一步。

## 前提

- 不需要 Docker，也不需要 Homebrew。
- 不需要管理员密码：两个服务都装在用户目录下，不写入系统目录。
- 需要 Xcode 命令行工具（编译 Redis 用），`xcode-select -p` 有输出即可。

## 一、PostgreSQL 17

下载 [Postgres.app](https://postgresapp.com/) 的单版本 17 安装包，解包后放进项目专用目录。
路径不能改——`scripts/local_postgres.py` 就是按这个位置写的。

```bash
ROOT="$HOME/Library/Application Support/AIJubenshaFusionTest"
mkdir -p "$ROOT"
# 从 Postgres.app 官方 GitHub 发布页下载 Postgres-2.9.6-17.dmg（约 114 MB）
hdiutil attach -nobrowse -readonly ~/Downloads/Postgres-2.9.6-17.dmg
ditto /Volumes/Postgres-2.9.6-17/Postgres.app "$ROOT/Postgres.app"
hdiutil detach /Volumes/Postgres-2.9.6-17
```

初始化数据目录，并限制为只监听本机回环：

```bash
BIN="$ROOT/Postgres.app/Contents/Versions/17/bin"
"$BIN/initdb" -D "$ROOT/pgdata" -U postgres --encoding=UTF8 --locale=C \
  --auth-local=trust --auth-host=scram-sha-256
chmod 700 "$ROOT/pgdata"
```

在 `$ROOT/pgdata/postgresql.conf` 末尾追加：

```
listen_addresses = '127.0.0.1'
port = 55432
```

把 `$ROOT/pgdata/pg_hba.conf` 整体替换为（只允许回环，TCP 要求密码）：

```
local   all   all                  trust
host    all   all   127.0.0.1/32   scram-sha-256
host    all   all   ::1/128        scram-sha-256
```

启动并建库。管理操作走本机 Unix socket（trust），应用连接走 TCP（需要密码）：

```bash
backend/.venv/bin/python scripts/local_postgres.py start

DBPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
"$BIN/psql" -h /tmp -p 55432 -U postgres -d postgres \
  -c "CREATE ROLE jubensha LOGIN PASSWORD '$DBPW';"
"$BIN/createdb" -h /tmp -p 55432 -U postgres -O jubensha -E UTF8 jubensha
echo "把这个密码填进 .env 的 DB_PASSWORD：$DBPW"
```

集成测试另需一个可随时丢弃的沙箱库，名字和用户名是测试脚本写死的：

```bash
ITPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
"$BIN/psql" -h /tmp -p 55432 -U postgres -d postgres \
  -c "CREATE ROLE fusion_it LOGIN PASSWORD '$ITPW';"
"$BIN/createdb" -h /tmp -p 55432 -U postgres -O fusion_it -E UTF8 fusion_pg_it_sandbox
```

## 二、Redis 7.4

官方只发布源码，用系统自带的 clang 编译即可，几十秒完成：

```bash
RROOT="$HOME/Library/Application Support/AIJubenshaFusionRedis"
mkdir -p "$RROOT/bin" "$RROOT/data" "$RROOT/log"
curl -fL -o /tmp/redis.tar.gz https://download.redis.io/releases/redis-7.4.2.tar.gz
mkdir -p /tmp/redis-src && tar xzf /tmp/redis.tar.gz -C /tmp/redis-src --strip-components=1
make -C /tmp/redis-src -j8 MALLOC=libc BUILD_TLS=no
cp /tmp/redis-src/src/redis-server /tmp/redis-src/src/redis-cli "$RROOT/bin/"
chmod 700 "$RROOT" "$RROOT/data"
```

写 `$RROOT/redis.conf`。**路径含空格必须加引号**，否则 Redis 会按多参数解析并拒绝启动；
`dir` 必须与 `scripts/local_redis.py` 中的 `DATA_DIR` 完全一致，否则该脚本会判定
端口上不是本项目实例而拒绝接管（这是它防止误停他人 Redis 的保护）。

```
bind 127.0.0.1 -::1
protected-mode yes
port 56379
daemonize yes
dir "/Users/<你>/Library/Application Support/AIJubenshaFusionRedis/data"
logfile "/Users/<你>/Library/Application Support/AIJubenshaFusionRedis/log/redis.log"
pidfile "/Users/<你>/Library/Application Support/AIJubenshaFusionRedis/redis-56379.pid"
save 300 10
appendonly no
```

```bash
backend/.venv/bin/python scripts/local_redis.py start
```

## 三、`.env`

以 `.env.example` 为模板复制，再按本机改这几项。已有 `.env` 时不要覆盖。

| 项 | 本机开发值 | 说明 |
| --- | --- | --- |
| `ENV` | `development` | 生产自检只在 `production` 下生效 |
| `SECRET_KEY` | 随机 48 字节 | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `DB_HOST` / `DB_PORT` | `127.0.0.1` / `55432` | 专用实例 |
| `DB_NAME` / `DB_USER` | `jubensha` / `jubensha` | |
| `DB_PASSWORD` | 上面生成的 `$DBPW` | |
| `REDIS_URL` | `redis://127.0.0.1:56379/0` | |
| `HOST` / `PORT` | `127.0.0.1` / `8010` | |
| `FILE_STORAGE` | `dir` | 用本机目录，不连 MinIO |
| `ALLOW_ANONYMOUS_ACCESS` | `true` | 开发期免登录，不必先接通短信 |
| `ENABLE_PAID_MODEL_CALLS` | `false` | 默认不产生任何费用 |

云模型 Key 一律留空——本机开发不调用云模型，填 `CHANGE_ME` 之类的占位符
反而会掩盖"忘了配"的问题。

```bash
chmod 600 .env   # .gitignore 已排除，不会被提交
```

## 四、建表

```bash
cd backend && ./.venv/bin/python -m alembic -c src/db/migrations/alembic.ini upgrade head
```

应看到 13 个迁移依次应用，最终版本 `o5b6c7d8e9f0`，public schema 下 28 张表。

## 五、启动与验收

```bash
cd backend && ./.venv/bin/python -B main.py          # 后端 127.0.0.1:8010
```

后端 API 地址 `http://127.0.0.1:8010`。前端界面当前已移除待重建，重建后再接入浏览器；访客模式下应能直接进入游戏主界面，不出现登录页。

数据库集成测试（会在沙箱库里创建临时 schema，用完即弃）：

```bash
cd backend
FUSION_POSTGRES_TEST_CONFIRM=CREATE_EPHEMERAL_SCHEMA \
FUSION_POSTGRES_TEST_DATABASE_URL='postgresql+psycopg://fusion_it:<ITPW>@127.0.0.1:55432/fusion_pg_it_sandbox' \
  ./.venv/bin/python scripts/test_fusion_postgres_budget.py

PACKAGE_PLAY_POSTGRES_TEST_CONFIRM=CREATE_PACKAGE_PLAY_EPHEMERAL_SCHEMA \
FUSION_POSTGRES_TEST_CONFIRM=CREATE_EPHEMERAL_SCHEMA \
FUSION_POSTGRES_TEST_DATABASE_URL='postgresql+psycopg://fusion_it:<ITPW>@127.0.0.1:55432/fusion_pg_it_sandbox' \
  ./.venv/bin/python scripts/test_package_play_postgres.py
```

## 日常启停

项目自带的脚本（VS Code 任务里也有对应按钮）：

```bash
backend/.venv/bin/python scripts/local_postgres.py start|status|stop
backend/.venv/bin/python scripts/local_redis.py    start|status|stop
```

"停止"只停服务，数据文件保留。两个脚本都会先核对数据目录，不会误停机器上别的实例。

## 这套环境的边界

- 只是开发环境。上线前仍需真实短信供应商、托管 Redis、域名与 TLS、数据库备份恢复和监控。
- 访客模式必须关掉才能上线——生产环境开着它，启动自检会拒绝启动。

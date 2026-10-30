# 腾讯云部署雾幕档案

适用于新建的腾讯云轻量应用服务器（Lighthouse）或云服务器CVM。完整运行Next.js、FastAPI、PostgreSQL、Redis和Caddy，模型仍调用外部API，不需要GPU。

代码仓库：https://github.com/zhang1845/veil-archive ，当前默认分支 `codex/initial-upload`。仓库为私有，首次克隆需要自己的GitHub授权；不要把个人访问令牌写进克隆URL。

## 1. 准备服务器与网络

建议测试服务器至少2核CPU、4GB内存、40GB磁盘；这只是起步建议，不保证并发容量。首次在服务器构建镜像需要额外临时磁盘空间。

在腾讯云控制台创建实例，选择Linux的Docker CE应用镜像/预装Docker的基础镜像，省去手工安装Docker。官方示例目前基于Ubuntu Server 24.04 LTS，具体以购买页面为准。已有服务器可以使用，但先确认80/443没有被现有网站占用。

- 对访客放行TCP 80、443；UDP 443仅用于可选HTTP/3。
- SSH 22只对自己的管理IP或所用腾讯云登录方式要求的来源放行。
- 不开放3001、8009、8010、5432、6379。
- 中国内地资源对外提供网站服务前按腾讯云要求完成ICP备案；中国香港及境外资源的ICP备案适用规则不同，参照官方文档选择地域。

通过腾讯云实例的“登录”或自己的SSH连接进入服务器。以下命令均在服务器终端执行，Docker命令用sudo以适配普通登录账号。

```sh
docker --version
docker compose version
```

Compose插件要求2.24.0或更新版本（本配置使用env_file.required），不使用旧版docker-compose v1。若镜像没有Compose插件，按Docker官方安装说明补齐，不使用陌生的一键脚本。

## 2. 拉取私有仓库

为这台服务器创建专用密钥。若指定文件已存在，不覆盖，改用另一个文件名并同步替换后续命令。

```sh
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -C "veil-archive-tencent" -f ~/.ssh/veil_archive_deploy
cat ~/.ssh/veil_archive_deploy.pub
```

只把 `.pub` 公钥复制到仓库的 **Settings → Deploy keys → Add deploy key**，不勾选写入权限。私钥保留在服务器，不能发到聊天或放入仓库。

然后克隆：

```sh
GIT_SSH_COMMAND='ssh -i ~/.ssh/veil_archive_deploy -o IdentitiesOnly=yes' git clone --branch codex/initial-upload git@github.com:zhang1845/veil-archive.git ~/veil-archive
cd ~/veil-archive
git config core.sshCommand 'ssh -i ~/.ssh/veil_archive_deploy -o IdentitiesOnly=yes'
```

SSH首次连接时核对GitHub官方主机指纹。如果未安装Git，可在Ubuntu上先执行 `sudo apt-get update` 和 `sudo apt-get install -y git python3`。GitHub SSH认证成功时 `ssh -T` 仍可能退出码1，因此不要用它的退出码作为克隆命令的前置条件。

## 3. 创建专用生产配置

在项目目录运行下面的Python。它生成随机密码和签名密钥，只写入权限0600的 `.env`，不显示秘密；如果 `.env` 已存在就拒绝覆盖。

```sh
python3 - <<'PY'
import os
import secrets
values = {
    'SITE_ADDRESS': ':80',
    'CORS_ORIGINS': '',
    'DB_NAME': 'veil_archive',
    'DB_USER': 'veil_archive',
    'DB_PASSWORD': secrets.token_urlsafe(32),
    'SECRET_KEY': secrets.token_urlsafe(48),
    'ACCESS_TOKEN_EXPIRE_MINUTES': '120',
    'REFRESH_TOKEN_EXPIRE_MINUTES': '43200',
    'ENABLE_PAID_MODEL_CALLS': 'false',
}
try:
    descriptor = os.open('.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit('.env already exists; keep it and edit deliberately.')
with os.fdopen(descriptor, 'w') as output:
    output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
print('Created private .env; no secrets were printed.')
PY
```

Compose固定使用生产模式、独立账号、关闭假短信/共享访客、本地试玩、旧管理API、模型费用与默认worker。启动基础网站无需模型密钥。不要为了看到本地剧本而把生产改成development。

## 4. 构建与启动

```sh
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1/health
```

初次构建需要下载镜像和npm/Python依赖。腾讯云Docker镜像源不等于npm/PyPI也已加速；下载失败时查看具体日志并使用可信源，不关闭TLS校验或使用随机公共代理。

五个默认服务应正常运行，frontend/backend健康检查应通过。数据库是新建的独立生产库，后端先进行安全自检与Alembic迁移。失败时使用：

```sh
sudo docker compose logs --tail=100 backend
sudo docker compose logs --tail=100 frontend
sudo docker compose logs --tail=100 caddy
```

不要在排错时运行 `docker compose down -v`，这会删除持久卷。也不要运行不带 `--quiet` 的配置打印命令后把内容贴到公开聊天，它会展开数据库密码。

此时可先用 `http://服务器公网IP` 验证首页。此IP不是127.0.0.1，后者只代表访问者自己的电脑。

## 5. 域名、HTTPS和账号

如果有域名，在DNS控制台添加A记录，例如 `game` 指向该服务器公网IPv4。若设置AAAA记录，还必须确保IPv6确实可达，避免证书验证走到错误地址。

确认地域相应备案要求和DNS解析完成后，编辑服务器 `.env` 中这两项，替换为自己的实际域名：

```dotenv
SITE_ADDRESS=game.example.com
CORS_ORIGINS=https://game.example.com
```

只改网站地址，不重新生成已有的DB_PASSWORD或SECRET_KEY。应用已有数据库卷时，随意换数据库密码会导致连接失败。

```sh
sudo docker compose up -d caddy
```

Caddy会为实际域名申请和续期HTTPS证书；DNS正确、80/443可达是前提。确认浏览器证书正常后再输入登录密码。先用IP调试首页不等于已经具备HTTPS。

后台健康后创建管理员。把示例邮箱替换为实际邮箱，密码按提示隐藏输入（至少12字符）：

```sh
sudo docker compose exec backend python scripts/bootstrap_account.py --username owner --email owner@example.com --role admin
sudo docker compose exec backend python scripts/bootstrap_account.py --username player1 --email player1@example.com --role player
```

脚本只新增账号，已有用户名/邮箱会拒绝，不能用它重置或升级已有账号。不要把密码放到命令参数中。

- 首页：`https://你的域名/`
- 玩家登录：`https://你的域名/player-login`
- 创作者登录：`https://你的域名/login`
- 工作台：`https://你的域名/admin`

没有域名时可以检查IP入口，但不要在明文HTTP里传真实登录凭据。可先准备域名，或仅通过可信的SSH本地端口转发测试账号流程。

## 6. 需要AI创作时再启用

基础网站启动不代表已经有可公开游玩的剧本。本机的LOCAL_TRIAL及数据库、私本和存档未被上传，正式剧本仍要真实模型审核、人工审核、确认与发布。

管理员准备好服务商Key后，在服务器现有 `.env` 中补充百炼白名单模型、实际单价、费率版本及预算（参照 `.env.example` 对应项，但不能用模板覆盖现有文件）。确认费用设置后显式改为 `ENABLE_PAID_MODEL_CALLS=true`。

当前原创模型配置读取 `/app/.env` 文件，因此AI专用override只读挂载服务器配置，不能把秘密COPY进镜像。需要明确选择本仓库附带的 `docker-compose.ai.yml`：

```sh
sudo docker compose -f docker-compose.yml -f docker-compose.ai.yml --profile creative up -d --build
```

该命令才会启动付费创作worker。其余情况不要启用creative profile。AI只能提交候选和审核发现，不能自动批准发布。未知调用不自动重发，先核对账本和服务商账单。

## 7. 后续更新与备份

```sh
cd ~/veil-archive
git pull --ff-only
sudo docker compose up -d --build
```

如果已启用AI，用同样的两个 `-f` 文件和 `--profile creative` 更新，保留原 `.env` 和卷。更新前备份数据库和对应的来源/核验报告/创作账本；只备份数据库不能恢复完整正式发布依据。

## 官方参考与验证边界

- [腾讯云Docker CE应用镜像](https://cloud.tencent.cn/document/product/1207/60423)
- [腾讯云备案资源地域要求](https://cloud.tencent.cn/document/product/243/18908)
- [GitHub Deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
- [Docker Compose插件](https://docs.docker.com/compose/install/linux/)

本轮完成生产配置、前端构建与隔离测试，尚未在腾讯云实例上实际运行Docker、迁移、DNS或证书流程。执行时以真实健康检查与网站结果为准，不把文档存在当作上线完成。

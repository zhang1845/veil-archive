# CloudBase 静态前端

在本目录执行构建。`NEXT_PUBLIC_API_URL` 是最终后端服务的 HTTPS 根地址，
不含 `/api` 后缀；浏览器请求会自行附加 `/api/...`。它是公开配置，不得放 API Key。

```sh
CLOUDBASE_STATIC_EXPORT=true NEXT_PUBLIC_API_URL=https://你的后端域名 npm run build
```

产物在 `out/`。静态模式默认 `basePath=/veil-archive`；如需更改，构建时设置
`CLOUDBASE_SITE_BASE_PATH=/其他安全子目录`。普通 standalone 模式路径前缀仍为空。
上传整个目录的**内容**到 CloudBase 静态托管的 `veil-archive/` 子目录，
不要覆盖共享环境的根 `index.html` 或其他网站。
包括 `_next/`、`index.html` 和各路由目录。各页面均输出目录索引，
例如 `veil-archive/scripts/index.html`、`veil-archive/play/index.html`、`veil-archive/admin/index.html`。
首页文档使用 `index.html`；管理端与玩家登录沿用原有后端鉴权。

如控制台接收 ZIP，先选中托管目标子目录 `veil-archive/`，再上传下列 ZIP，
避免多包一层 `out/`。如果只能上传到托管根目录，则 ZIP 必须包含
`veil-archive/` 外层目录，严禁把此 ZIP 直接展开到共享根目录。

```sh
cd out
zip -r /tmp/veil-archive-cloudbase-web.zip .
```

后端 CORS 必须显式允许该静态站点的 HTTPS origin。更换后端地址后须重建，
不能仅修改云托管运行时环境变量来替换已编译进前端的公开地址。
静态导出不能用 `next start`；本地可用任意静态文件服务器检查 `out/`。

不设置 `CLOUDBASE_STATIC_EXPORT=true` 时，仍构建原有 standalone 产物，
保留现有 Docker/Next.js 部署方式。

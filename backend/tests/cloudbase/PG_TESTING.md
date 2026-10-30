# 独立 PostgreSQL 验证

`test_pg_transactions.py` 不读取 `.env`，不导入应用或旧 `conftest`。默认跳过；只有 `VEIL_CLOUDBASE_TEST_DSN` 与脚本内专用 Unix socket DSN 完全一致才启用，连接后再次检查 `data_directory` 和禁用 TCP。脚本只创建本任务的 `veil_cloudbase_test` 数据库及三个模拟 CloudBase 角色，再应用 `001`—`005`。

当前本机已有测试 cluster 位于仓库外 `cloudbase-free-prep/pg-test/data`，socket 为该目录旁 `socket`，权限 0700、端口 55482、临时超级用户 `veil_test`。**不得把 DSN 换成开发/生产库来跑此测试。** 在其他机器执行应先建立独立测试 cluster，并同步修改脚本中的 guard；不要绕过 guard。

从项目根执行：

```sh
VEIL_CLOUDBASE_TEST_DSN='host=/Users/zhanglifu/Documents/Codex/veil-upload-prep/cloudbase-free-prep/pg-test/socket port=55482 user=veil_test dbname=postgres' \
PYTHONPATH=backend backend/.venv/bin/python -m unittest discover -s backend/tests/cloudbase -p 'test_pg_transactions.py' -v
```

`backend/.venv` 在此新工作树未安装时，可明确使用已有开发解释器运行，不复制真实环境配置。不要运行旧全量 pytest/conftest。

测试覆盖实际独立 PostgreSQL 的角色权限、函数体校验、并发 create/CAS、幂等回执重放、访客隔离/过期、refresh 轮换争抢、logout、并发限流、任务 lease/fence、费用预占/单次 dispatch、完成幂等、退款单次生效及未知调用保守计费。也覆盖正式发布依赖的原子校验、撤销先后顺序的双向阻塞、有序目标/依赖锁防 AB/BA 死锁、撤销后的原回执重放，以及任务取消的管理员检查、幂等和未知费用保护。测试只做合成账本变更，没有模型客户端或网络云调用；每项结束把测试库的 paid_execution_enabled 还原为 false。

这些测试不模拟 CloudBase 网关验签、真实 Key 授权、Storage API 错误格式或平台并发/限额。存储 HTTP 协议和故障用 `test_private_storage.py` 配合真实 gateway+假 transport 验证。实际云环境需另外执行隔离的上传/读回/权限和 RPC 验收。

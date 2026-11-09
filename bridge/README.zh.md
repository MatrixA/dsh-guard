# dsh-guard Host bridge

[English](README.md)

`@local/dsh-guard-bridge` 是适配 DSH **0.2.2-rc.1** 的可选文件系统 bundle，为外部 [dsh-guard 守护器](../README.zh.md)提供有界的 Host 活动与能力状态。这是独立社区项目的一部分，与 DeepSeek 无官方隶属关系。

bridge 观察原生插件操作与 HMR，不安装插件、不发布服务、不替换 HMR，也不修复源码。它不发布服务，因此组合 row 无需 `isolate` realm。该包标记为 private，用于本地原生安装，不表示已经发布到包注册表。

## 安装并启用开发模式

受支持的 DSH runtime 需要 Python 3.11+ 和 Node.js **24.9.0 或更高版本**。通过 DSH 原生 Plugin Manager 或插件 CLI 安装并启用本地 bundle，例如：

```sh
dsh plugin --profile web add 'link:/absolute/path/to/dsh-guard/bridge'
dsh --profile web --dump-config
```

应选定正确的 DSH home 后执行。原生组合准备可能写入 profile 文件；评估陌生安装时使用可丢弃 home。profile 的 `cordis.yml` 保持 `[]`，人工编辑写在 `cordis.patch.yml`。确认 bundle 已选中，且 row 出现在最终 Host 组合树中。本地 link 不会开启源码监听，原生 HMR roots 需要另行配置。

之后按 [macOS 安装指南](../docs/macos.md)，使用 `macos_setup.py --development` 登记，显式设置 Guard 配置中的 `development_mode`。安装器要求 bridge 已经被选中，但仍然只生成观察模式安装。基线准备、验证、启动监督循环和管理交接是分开的步骤。仅安装 bundle 不会启用恢复，也不会接管正在运行的 DSH 进程。

Guard 启动受管开发进程时，会同时提供 `DSH_GUARD_BRIDGE_FILE` 和 `DSH_GUARD_EPOCH`。缺少有效绝对输出路径或非空 epoch 时，bridge 不工作。在这些环境变量建立前启动的进程，不会因安装 bundle 就变成受管进程。

交接完成后，要求 bridge 新鲜且 idle、全部能力 ready 或 disabled，再创建并查看首个恢复点：

```sh
dsh-guard checkpoint
dsh-guard status
```

bridge 或任何上报能力缺失、等待或失败时，核心就绪接口成功也不足以接受恢复点。Windows 守护预览不安装此 bridge，也不启用恢复点流程；Linux 和 WSL 不支持。

## 观察协议

观察器约每两秒输出一次 schema-1 JSON，使用同目录私有临时文件和原子 rename。它只投影自身持有的标量记录，不序列化原生运行时对象或诊断正文。

| 字段 | 含义 |
|---|---|
| `pid`、`epoch`、`updated_at` | 关联 Guard 启动的进程及报告新鲜度 |
| `phase` | 根据已观察操作与能力状态给出的 `busy`、`idle` 或 `error` |
| `generation` | 新观察器 activation 与 reconcile/reload 事件序列 |
| `operation` | 有界的最新或进行中操作摘要，保留不确定结果 |
| `capabilities` | Loader row、patch 语法、bundle 清单，状态为 ready/disabled/waiting/failed，并标记核心必需项 |
| `restart_intent` | 始终为 `null`，不推断缺少可靠原生通知的计划重启意图 |

原生 plugin-manager 操作事件覆盖管理器工作与锁等待。根 include reconciliation 和 HMR 事件范围更窄：continuation 成功不证明整个事务已完成。generation **不是原生 commit 标识**，bridge 无法保证在每次文件写入前建立快照。计划重启应使用 Guard 命令或控制台。

原生 continuation 的错误原样向上传播。观察器自身错误被限制在观察器内，并表现为 error 或过期状态。bundle 清单读取挂起会转为 waiting，不会停止心跳输出。卸载时等待进行中的写入结束，保留最后报告供 Guard 判断过期。接受恢复点前，监督器还要核验进程身份、epoch、新鲜度、原生 writer、稳定文件指纹，并独立验证候选。

## 测试

不依赖 DSH 的观察器检查使用 Node 内置测试运行器；在仓库根目录执行：

```sh
node --test bridge/test-bridge.mjs
```

可选的既有安装契约检查为：

```sh
DSH_GUARD_TEST_INSTALL_ANCHOR=/absolute/dsh-runtime/node_modules/@deepseek-ai/dsh/package.json \
  node --expose-internals --test bridge/test-bridge.mjs
```

不设置该环境变量时，原生契约检查明确跳过。它只挂载临时 profile，不安装包、不调用外部端点、不控制生产服务。通过仅证明被测原生 bundle/parser/event 契约，不能代表所有 DSH 业务集成。先前验收和未覆盖范围见 [ACCEPTANCE.md](../ACCEPTANCE.md)。

bridge 原创代码采用项目的 [MIT License](../LICENSE)，上游依赖保留各自许可证。分享诊断前请阅读[安全策略](../SECURITY.md)。

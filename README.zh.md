# dsh-guard

[English](README.md)

dsh-guard 是 [DeepSeek Harness（DSH）](https://github.com/deepseek-ai/deepseek-harness) 的进程外守护程序。监督和恢复运行在被修复的应用之外，保留原生插件管理与热加载，恢复候选经过独立验证后才会被接受。

这是 DeepSeek Harness 生态中的独立社区项目，并非 DeepSeek 官方项目，与 DeepSeek 无隶属或背书关系。

## 当前状态

| 平台 | 范围 | 验证情况 |
|---|---|---|
| macOS，DSH **0.2.2-rc.1** | 进程守护、已验证恢复点、独立救援 home、返回开发环境、可选受限修复 | 先前本机进程、开发流程和浏览器验收通过；通用安装另经临时 fixture 与独立原生 DSH home 验证 |
| 原生 Windows | 有限进程守护预览及验收脚本 | **尚未在原生 Windows 测试**；候选隔离与恢复尚未实现，Claude 修复禁用 |
| Linux / WSL | 不支持 | 没有验收结论 |

详见[验收范围](ACCEPTANCE.md)和 [Windows 预览说明](WINDOWS.md)。既有部署的验收不能证明全新安装或其他 DSH 版本可用。

## macOS 安装

先阅读[环境要求](docs/environment.md)和 [macOS 安装与操作](docs/macos.md)。需要 Python 3.11+、持久目录中的 DSH runtime，以及 Node.js **24.9.0 或更高版本**的绝对可执行路径：

```sh
python3 macos_setup.py --runtime /absolute/dsh-runtime --node /absolute/node
```

安装器默认使用 `DSH_HOME` 或 `~/.dsh`，Guard 文件遵循 XDG 目录设置，生成观察模式安装、LaunchAgent plist 和稳定 CLI。它不会准备 release、加载 launchd 或接管已有 DSH 监听者。启用管理前，应核对生成的安装，并按 macOS 指南准备和验证基线。

安装后的 CLI 提供：

```sh
dsh-guard status
dsh-guard stop
dsh-guard resume --manage
dsh-guard restart
dsh-guard checkpoint
dsh-guard return-development
```

`resume --manage` 需要明确交接和可用的已验证基线。`checkpoint` 与 `return-development` 要求开发模式。人工停止跨守护器重启保留。计划重启应使用 Guard 命令或控制台；直接发送退出信号不能证明原生计划重启意图。

监督循环运行时，独立控制台默认位于 <http://127.0.0.1:3081>，分别展示应用就绪、Guard 心跳、待验证改动与操作结果。“打开 DSH”会先检查受管进程和监听归属，再进行本机认证跳转。

## 开发与恢复

日常继续使用 DSH 原生 Plugin Manager、profile patch 和源码 HMR。需要保留可变开发工作区时，先通过原生插件管理安装可选 [Host bridge](bridge/README.zh.md)，再在安装时加入 `--development`，显式设置 `development_mode`。基线准备、验证、交接与首个恢复点仍需按 macOS 指南单独完成。安装器默认采用固定模式。仅建立本地包链接不会启用源码监听，还需要单独配置原生 HMR roots。

开发模式冻结插件声明的发行产物，保留正在使用的开发工作区。只有原生 writer 空闲、能力全部 ready 或 disabled、独立沙箱检查通过，且文件指纹与观察代际保持稳定，恢复点才会被接受。配置错误或可选插件故障可以推迟恢复点，而不打断仍健康的进程。

有限重启仍失败时，可用同版本已验证恢复点启动独立救援 home。原开发配置和坏源码留在原处供修复。返回开发环境需要再次独立验证并检查实际启动。会话、存储、凭据和数据库不回滚；进程切换可能中断正在执行的任务，也不会自动重放 Agent 的外部操作。

## 能力边界

- 仅管理登记的 `web` profile 与配置的监听端口；不会收养或终止未知监听者。
- 候选验证依赖原生 macOS 隔离及正反检查；scratch 验证不能证明所有外部模型、MCP、IM 或浏览器集成的业务调用可用。
- macOS 的可选 Claude 修复默认关闭，只接受已登记策略和 incident 预算内、受 Schema 约束的提案，不能任意修改源码或执行工具。Windows 在执行入口拒绝修复。
- 稳定 launcher 校验安装版本，并限制心跳恢复的次数与时限；无法修复自身 bootstrap、缺失的解释器或操作系统。同账户进程不在强隔离保证之内。
- LaunchAgent 需要用户登录；睡眠和关机中断服务。跨 DSH 版本的数据迁移不支持。

另见[实现说明](IMPLEMENTATION.md)、[贡献指南](CONTRIBUTING.md)和[安全策略](SECURITY.md)。

## 许可证

本项目原创代码采用 [MIT License](LICENSE)。DSH、Node.js、插件及其他上游软件分别保留其自身许可证。

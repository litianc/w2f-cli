# Windows 前置工作

Windows 端接入的机器级前置条件与开机自启说明。部署命令与 macOS 一致（通用流程见 [`新机器部署前置准备.md`](新机器部署前置准备.md)），本文只列 Windows 的差异与日常手动项。

## 每次开机后需要手动做的事

w2f 的开机自启只恢复自己的三件套（管理台、watchdog、运行中的绑定 Worker）。以下两项**不会**被自动拉起，需要手动：

1. **登录桌面微信**（Windows 客户端）。
2. **启动 WeChatDataAnalysis（WDA）**——w2f 的 Windows 读取依赖，默认 API 地址 `http://localhost:10392`。

WDA 未启动时绑定 Worker 仍会存活并心跳，读取侧标记「断线重连」；WDA 起来后自动按序续传，不丢消息。是否把 WDA 和微信加入系统启动项由你自己决定，w2f 不代管。

检查是否就绪：

```bash
python -m agents.manage status        # Worker alive、心跳、游标、积压
python -m agents.manage metrics       # 阶段2 验收指标（时延/媒体成功率）
```

## 首次部署前置

- Python 3.10+、Git；`python -m venv .venv` 后安装 `requirements.txt`。
- 桌面微信已登录。
- [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis) 已安装，并确认能与桌面微信同时运行。
- `lark-cli` 与飞书应用权限（权限清单与接入记录见 [`飞书应用接入记录.md`](飞书应用接入记录.md)）。
- 读取器适配层安装：`.venv\Scripts\python scripts\install_wda_reader.py`——生成机器本地的 `integrations/wda_reader.cmd`（含本机 venv 路径，不入库）。

## 开机自启（w2f-autostart 计划任务）

| 操作 | 入口 |
| --- | --- |
| 注册 | 管理台「运行概览」→「启用开机自启」，或 `python -m agents.manage autostart --register` |
| 注销 | 同位置「注销开机自启」，或 `python -m agents.manage autostart --unregister` |
| 查看状态 | 同位置状态徽章，或 `python -m agents.manage status` 旁的 `GET /api/autostart` |

行为说明：

- 任务名 `w2f-autostart`，**登录触发**、当前用户身份、`pythonw` 无窗口运行；是计划任务，**不装系统服务**。
- 登录后自动恢复：管理台、watchdog，以及重启前控制状态为 **running** 的绑定。stopped / paused / archived 一律不拉起。
- 幂等：重复注册/重复运行安全（管理台按活动会话跳过，watchdog 有单例锁，绑定启动本身幂等）。
- **注册保存的是当前解释器与配置的绝对路径**：移动仓库、换 Python 或换数据目录后需要重新注册。
- WDA 与桌面微信**不在**自启范围内（见第一节）。

## 重启验收流程

改动了开机自启相关代码，或首次注册后，建议做一轮真实验证：

1. 注册状态下正常重启一次：登录后约一分钟内 `python -m agents.manage status` 应显示 Worker alive、watchdog 在岗、游标连续无跳变。
2. `--unregister` 后再重启一次：不应出现任何 w2f 自动拉起的进程。

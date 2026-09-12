# 微信 → 飞书会话镜像

Python、SQLite、微信读取 CLI 和飞书 CLI；原生 HTML/CSS/JavaScript 本地管理台。Codex 负责启动、停止、查询和排障，Worker 负责确定性的读取与发送。不安装开机任务或异常自动重启服务。

## 安装与首次接入

1. 准备 Python 3.10+，执行 `python3 -m pip install -r requirements.txt`。视频封面需要本机已安装 `ffmpeg`，缺少时使用文件附件。
2. 按微信读取 CLI 的首次接入流程授权本机只读访问，并确认 `reader.sh access-plan` 可读。
3. 为已安装读取器增加完整分页扩展：`python3 scripts/install_reader_sync.py /实际路径/rion_wechat_reader.py`。脚本保留引擎备份，只添加 `sync-page`，不修改微信数据库；重新安装读取器后需要重新应用。可用 `WECHAT_READER` 指定兼容入口。
4. 安装并准备 `lark-cli`。在管理台发现已有 Profile，或通过接入表单将 Secret 经 stdin 交给 CLI；项目只保存 App ID 和 Profile 引用。
5. 在项目目录执行 `python3 -m agents.manage serve`，通过它返回的本机地址打开接入引导。完成群搜索、稳定 ID、应用和管理员信息，再检查并初始化。打开网页、保存草稿和初始化都不会自动开始镜像。
6. 明确选择某一条绑定启动。首次从当前最新位置开始；之后的启动和恢复会补齐离线期间的消息。

默认配置是 `~/.config/chz-cli/wechat_feishu_mirror.json`，SQLite、日志、媒体、备份和管理会话位于 `~/.local/share/chz-cli/`。`config/wechat_feishu_mirror.json` 只是空白示例。可通过 `CHZ_CONFIG_DIR`、`CHZ_DATA_DIR` 使用隔离环境。

管理员私聊提醒需要在其所属 `admin_profile` 下核实 `admin_feishu_user_id`，不能复制另一应用的 open_id。当前已有绑定的管理员配置由 Codex 修改本地配置；网页“保存进度”只保存引导草稿。

## Codex 常用命令

```bash
python3 -m agents.manage status
python3 -m agents.manage profiles list
python3 -m agents.manage bindings list
python3 -m agents.manage people list
python3 -m agents.manage confirmations list
python3 -m agents.manage bindings start --id 1
python3 -m agents.manage bindings pause --id 1
python3 -m agents.manage bindings resume --id 1
python3 -m agents.manage bindings stop --id 1
```

命令返回 JSON，和网页使用同一数据库与控制逻辑。每组绑定单独持有进程锁、游标及心跳。停止或异常退出后，重新执行 `start`；`resume` 只恢复仍在运行的暂停 Worker。

恢复时先完整分页读取固定区间，再按源时间与 local_id 依次发送，完成后才提交游标。媒体缺失时按原位置占位，后续人工修复回复该占位。发送回执不确定且超过平台去重窗口时暂停等待核对；源历史删除或重置时保留位置并报错。

## 验证与交付边界

```bash
python3 -m unittest discover -s tests -v
node --check agents/web/app.js
python3 scripts/check_public_tree.py
git diff --check
```

测试在临时目录中使用模拟读取器/飞书 CLI，涵盖真实 Worker 子进程、10 组并行、5,500 条源记录、断线、重启、分段回执、媒体占位和迁移回滚，不发送到真实群。

详细要求见 [开发计划](docs/微信到飞书群镜像同步计划.md)、[验收表](docs/微信飞书镜像同步验收表.md) 和 [本阶段验收报告](docs/版本说明-v0.3-恢复专项验收.md)。新格式加密图片、转发记录内媒体及超大文件仍需后续适配；原图片缺失不能通过补发算法重建。

提交检查仅扫描当前文件。旧 Git 历史中仍存在早期个人接入记录和配置，公开推送前需要单独清理历史或从脱敏工作树新建公开仓库；本项目不会自动推送或重写历史。

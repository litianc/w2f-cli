# 微信飞书镜像 Agent 运行规范

## 运行职责

同步程序是项目内的 Python worker，不是常驻的 LLM 对话。Codex 负责按用户指令管理它：启动、停止、暂停、恢复、查询状态和处理异常。暂不配置 LangChain 或 macOS `launchd` 开机启动。

用户发现 worker 退出后，告诉 Codex“启动微信飞书同步 Agent”；Codex 先查询状态，再启动唯一实例。Codex 不在没有用户指令时自行重启进程。

## Codex 管理流程

当前方案中的 Codex 是按需参与的管理员。尚未配置持续巡检或事件唤醒，因此不能将它描述为已经持续运行的守护进程。正式 worker 已实现本节所需的进程控制和状态持久化；管理私聊入口、身份确认编排和补偿扫描仍在后续迭代。

### 一次启动会话

1. 用户告诉 Codex“开始镜像 示例微信群”。
2. Codex 读取项目运行规范、群绑定和状态，核查现有进程、微信读取能力及飞书 profile 可用性。
3. 如果已有实例，Codex 返回该实例的状态；如果没有实例，Codex 调用正式版 `start` 控制接口。
4. Worker 获取独占锁并启动读取，向 SQLite 写入心跳、游标和状态。
5. Codex 再次调用 `status`，确认启动结果，将源群、目标群和运行状态反馈给用户。
6. 启动会话结束后，Worker 持续执行消息同步并记录结果。

### 一次维护会话

| 用户意图 | Codex 执行的管理动作 | 结果核验 |
| --- | --- | --- |
| 暂停同步 | 调用 `pause` 控制接口 | 查询到已暂停，当前发送操作已结束 |
| 恢复同步 | 查询状态并调用 `resume` | 从保存位置恢复处理，已有成功记录不重复发送 |
| 查询状态 | 读取进程状态、心跳、游标、队列和失败项 | 用可读语言说明是否运行及待处理问题 |
| 进程异常后重新启动 | 检查退出原因和独占锁，再调用 `start` | 确认唯一实例、有效心跳和恢复位置 |
| 某条媒体失败，协助排查 | 读取对应失败记录，检查下载、格式、上传错误 | 给出原因；按用户指令修复或手动补发并核查结果 |
| 确认或更正某人的姓名 | 通过正式版身份管理接口更新映射 | 后续消息采用确认后的姓名 |

管理动作也应记录到 SQLite，包括动作、目标绑定、执行时间和结果，以便查询暂停、恢复和人工补发的历史。

### 运行期间的异常入口

- Worker 负责记录媒体失败、读取异常和未知身份，不依赖 Codex 当前是否正在会话中。
- 未知身份由 Worker 记录待确认项并使用昵称继续转发；Codex 管理会话负责通过飞书私聊请求确认并更新映射。
- Codex 在用户发起维护会话时读取这些记录并处理；SQLite 中新增待办本身不会自动唤醒 Codex。
- 飞书私聊回复需要关联到对应的身份确认请求，并由 Codex 核对后更新映射；关联不确定时请求用户澄清，不能仅凭已发送私聊就视为确认流程完成。

## 控制接口

正式版统一使用以下入口，所有命令都带绑定配置：

```bash
python agents/wechat_feishu_mirror.py status \
  --config config/wechat_feishu_mirror.json

python agents/wechat_feishu_mirror.py start \
  --config config/wechat_feishu_mirror.json

python agents/wechat_feishu_mirror.py pause \
  --config config/wechat_feishu_mirror.json

python agents/wechat_feishu_mirror.py resume \
  --config config/wechat_feishu_mirror.json

python agents/wechat_feishu_mirror.py stop \
  --config config/wechat_feishu_mirror.json
```

正式 worker 已提供上述子命令；Codex 负责在用户请求时调用它们、读取 SQLite 状态并反馈结果。

`status` 至少返回：进程是否存活、当前游标、最近心跳、最近成功消息、待发送数量、媒体失败数量、未知身份数量和最近错误。

## 暂停与恢复

- `pause` 将绑定状态写入 SQLite，并记录操作者和原因。
- worker 检测到暂停后停止读取和发送，不推进游标。
- 暂停期间微信消息保留在数据库中，恢复后从最后成功位置继续处理。
- `resume` 只恢复消费，不重新发送已成功记录的消息。

## 防止多进程

正式版采用双重保护：

1. 启动时对绑定级 lock 文件执行非阻塞 `flock`；已有 worker 时直接返回“已运行”。
2. SQLite 保存 worker 租约、PID、启动时间和心跳。状态查询发现租约失联后，Codex 才可以在用户指令下清理陈旧租约并启动新实例。

消息表的唯一键使用 `wechat_talker + local_id`，发送键使用同一消息的稳定哈希，避免重启、补偿扫描或人工重试造成重复消息。

## SQLite 最小模型

```text
bindings              微信 talker、飞书 chat_id、启用状态
runtime_control       running/paused、操作者、原因、更新时间
worker_leases         PID、启动令牌、心跳、退出时间
messages              微信消息、发送状态、飞书 message_id、错误和重试信息
sender_mappings       微信 ID、昵称、确认姓名、confirmed/pending 状态
media_jobs             媒体类型、staging 文件、上传状态、错误和人工处理状态
```

## 未知发言人

发现未知 ID 后：

1. 先使用微信昵称同步；没有昵称则使用“未知发言人”。
2. 通过飞书私聊向用户发送确认请求。
3. 用户确认后写入 `sender_mappings`，只影响后续消息。
4. 不因为一个未知 ID 阻塞其他成员消息。

## 媒体失败

普通图片、视频、文件和语音先下载到项目内 staging，再上传飞书。下载或上传失败时立即发送“姓名：[图片上传失败]”等占位文本，并在 `media_jobs` 中留下失败记录，供人工排查和手动补发。日志不记录带密钥的原始媒体 XML 或完整 CDN URL。

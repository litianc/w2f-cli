# README 展示素材

本目录仅保存用户指定用于项目介绍的成片、预览及已审阅的脱敏截图。原始截图、配乐工程素材、SQLite 和日志继续保存在本机数据目录。

| 文件 | 用途 |
| --- | --- |
| [w2f-cli-preview.mp4](w2f-cli-preview.mp4) | W2F CLI 29 秒宣传片，1920 × 1080、30 fps，H.264 + AAC，包含配乐 |
| [w2f-cli-preview.gif](w2f-cli-preview.gif) | 同一视频的无声预览，适合在 GitHub README 中展示 |
| [wechat-sanitized.png](wechat-sanitized.png) | 微信真实截图：已替换群名、姓名、头像，隐去时间 |
| [feishu-sanitized.png](feishu-sanitized.png) | 飞书真实截图：已替换群名、姓名和机器人显示信息，隐去时间 |

聊天画面采用用户提供的真实截图，群名、发送人姓名和头像已替换，对话与表情保留。两端统一采用“小林 / 小周”，保留姓名映射的展示效果。管理台、游标和运行数字使用合成演示数据。脱敏 PNG 为重新编码的扁平 RGB 图，不带原始元数据或可恢复图层。由 Remotion 制作运镜与字幕。该演示不是实时同步延迟测试，当前配置为每 10 分钟检查一次。

README 使用相对路径引用，克隆仓库后仍可查看。动图可直接展示；带配乐版本通过 MP4 链接打开或下载。未上传第三方视频平台。

`scripts/check_public_tree.py` 仅允许已审阅的这四个具体文件及其 SHA-256 值。替换成片时，先复核视频画面和元数据，再更新校验值；其他运行时媒体仍会被检查器拒绝。

制作方式见 [Remotion 工程说明](../../promo/README.md)。

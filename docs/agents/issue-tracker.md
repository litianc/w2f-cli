# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues: `litianc/w2f-cli`（私有仓库）。

## 本机无 gh CLI —— 用 REST API

这台机器没有安装 `gh`。所有 issue 操作用 GitHub REST API + curl 完成。认证令牌来自 Git 凭据管理器，**只进 shell 变量，绝不打印、不写日志、不进提交**：

```bash
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')
```

| 操作 | 调用 |
| --- | --- |
| 列 open issues | `GET /repos/litianc/w2f-cli/issues?state=open` |
| 读单个 issue | `GET /repos/litianc/w2f-cli/issues/<n>`（评论：`GET .../issues/<n>/comments`） |
| 创建 | `POST /repos/litianc/w2f-cli/issues`，JSON `{"title":…, "body":…}` |
| 评论 | `POST /repos/litianc/w2f-cli/issues/<n>/comments` |
| 关闭 / 重开 | `PATCH /repos/litianc/w2f-cli/issues/<n>`，`{"state":"closed"}` / `{"state":"open"}` |
| 标签 | 创建时 `{"labels":[…]}`；后加 `POST .../issues/<n>/labels` |

**中文正文必须走 UTF-8 文件**：Git Bash 向 curl 传中文参数按本地代码页（GBK）编码，服务端会乱码。先把内容包成 JSON 文件再 `--data-binary @file`：

```bash
PYTHONIOENCODING=utf-8 python -c "import json;print(json.dumps({'body':open('note.md',encoding='utf-8').read()},ensure_ascii=False))" > note.json
curl -s -X POST -H "Authorization: token $TOKEN" -H "Content-Type: application/json" \
  --data-binary @note.json https://api.github.com/repos/litianc/w2f-cli/issues/<n>/comments
```

## 隐私规则

Issue 会发布到 GitHub（即使仓库私有）：真实群 ID（`数字@chatroom`）、wxid、db_key、App Secret 一律不得出现在 issue 的标题、正文、评论中。仓库的 `scripts/check_public_tree.py` 只覆盖 Git 内文件，不覆盖 issue 内容——靠这条规则把关。

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests.)_

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

`GET /repos/litianc/w2f-cli/issues/<n>`，需要上下文时再取 `/comments`。

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: 单个 issue，label `wayfinder:map`，body 持有 Destination / Notes / Decisions-so-far / Fog 四节。创建：`POST .../issues` 带 `"labels":["wayfinder:map"]`。
- **Child ticket**: `POST .../issues` 创建，body 顶部写 `Part of #<map>`；随后挂为 sub-issue：`POST .../issues/<map>/sub_issues`，`{"sub_issue_id":<db-id>}`（db id 是 `GET .../issues/<n>` 返回的 `id` 字段，不是 `#序号`）。Label：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。**Claim = 认领**：`PATCH .../issues/<n>`，`{"assignees":[<login>]}`，是会话的第一个写操作。
- **Blocking**: 优先 GitHub 原生依赖：`POST .../issues/<child>/dependencies/blocked_by`，`{"issue_id":<blocker 的 db id>}`；端点不可用时回退正文首行 `Blocked by: #<n>, #<n>`。`GET .../issues/<n>` 响应的 `issue_dependencies_summary.blocked_by`（仅 open blockers）即活的前沿闸门。
- **Frontier query**: map 的 open 子票中，剔除有未关闭 blocker 或已有 assignee 者；按 map 顺序取第一。
- **Resolve**: `POST .../issues/<n>/comments` 发布答案 → `PATCH` 关闭 → 把一行决策摘要（gist + 链接）追加到 map 的 Decisions-so-far。

> 历史注记：`.scratch/w2f-windows/` 是配置本 tracker 之前的本地 effort 地图（w2f Windows 本机化），保留作参考；新的 effort 一律走 GitHub。

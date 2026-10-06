# AGENTS.md

w2f-cli（微信→飞书镜像，Python）：Windows 用 WDA 适配层，macOS 用 rion 读取器，读取器契约 ordered_sync_v1 是读端与发送端的解耦点。产品方向见 docs/产品终局Spec.md。

## Agent skills

### Issue tracker

GitHub Issues（litianc/w2f-cli，私有；本机无 gh CLI，用 REST API + git 凭据令牌）。See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context：根 CONTEXT.md + docs/adr/（惰性创建，不预建）。See `docs/agents/domain.md`.

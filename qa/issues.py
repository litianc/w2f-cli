"""Conservative GitHub publishing for repeatable, isolated functional failures.

No raw process output, trace, environment, or arbitrary report fields are sent to
GitHub. Publication is deliberately fail-closed: an uncertain API result never
triggers a fallback create or an automatic retry.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path


DEFAULT_REPO = "litianc/w2f-cli"
_CHECK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,239}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_RAW_OUTPUT = re.compile(
    r"Traceback\s*\(|\b(?:stdout|stderr|environ|environment|traceback|raw_log|raw_trace)\s*[:=]"
    r"|(?:^|\n)\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*=",
    re.IGNORECASE,
)


def redact(value, limit=500):
    """Accept prose only; remove common secrets, identities and local paths.

    This is a second boundary, not a license to collect real chat data. Callers
    must supply synthetic fixture summaries rather than logs or exception text.
    """
    if not isinstance(value, str):
        return "〔非文本内容已省略〕"
    if _RAW_OUTPUT.search(value):
        return "〔原始日志或环境信息已省略；请查看本地测试产物〕"
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"https?://[^\s<>]+", "〔链接已省略〕", value, flags=re.I)
    value = re.sub(
        r"\b(?:Bearer|Basic)\s+[^\s,;]+", "〔认证信息已脱敏〕", value, flags=re.I
    )
    value = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+"
        r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b",
        "〔凭证已脱敏〕", value,
    )
    value = re.sub(
        r"(?i)\b(?:[a-z_]*(?:token|secret|password|passwd|api_key|authorization|cookie)|"
        r"(?:chat|user|open|union|message|app|sender|receiver|server|person|account)_id)"
        r"\b[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        "〔敏感字段已脱敏〕", value,
    )
    value = re.sub(r"\b(?:wxid_|oc_|ou_|on_|om_|cli_)[A-Za-z0-9_-]+\b", "〔标识已脱敏〕", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "〔邮箱已脱敏〕", value)
    value = re.sub(r"(?<!\w)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\w)", "〔电话已脱敏〕", value)
    value = re.sub(r"(['\"])(?:[A-Za-z]:[\\/]|/)[^\r\n]*?\1", "〔本地路径已脱敏〕", value)
    value = re.sub(r"(?<!\w)[A-Za-z]:[\\/][^\s\"'<>]+", "〔本地路径已脱敏〕", value)
    value = re.sub(
        r"(?<![\w:])(?:~?/)(?:Users|home|private|tmp|var|opt|etc|root|workspace|workspaces|mnt|Volumes)"
        r"(?:/[^\s\"'<>]*)?", "〔本地路径已脱敏〕", value,
    )
    value = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
                   "〔标识已脱敏〕", value)
    value = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "〔长标识已脱敏〕", value)
    value = " ".join(value.split())[:limit]
    return value or "未提供"


def _prose(value, limit=500):
    # Public report strings are data, never GitHub Markdown, mentions, or HTML.
    value = redact(value, limit).replace("@", "＠").replace("<", "＜").replace(">", "＞")
    for char in "\\`*_{}[]#!|":
        value = value.replace(char, "\\" + char)
    return value


def _confirmed(check):
    if not isinstance(check, dict) or check.get("status") != "confirmed_failure":
        return False
    if check.get("consistent") is False:
        return False
    attempts = check.get("attempts")
    if not isinstance(attempts, list) or len(attempts) < 2:
        return False
    signatures = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("status") != "failed":
            return False
        signature = attempt.get("signature")
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            return False
        signatures.append(signature)
    return len(set(signatures)) == 1


def _public_check(check):
    check_id = check.get("id")
    if not isinstance(check_id, str) or not _CHECK_ID.fullmatch(check_id):
        raise ValueError("invalid_check_id")
    if check.get("category") not in ("bug", "enhancement"):
        raise ValueError("invalid_category")
    for field in ("title", "expected", "actual"):
        if not isinstance(check.get(field), str) or not check[field].strip():
            raise ValueError("invalid_" + field)
    evidence = check.get("evidence", [])
    if not isinstance(evidence, list) or any(not isinstance(item, str) for item in evidence):
        raise ValueError("invalid_evidence")
    related = check.get("related_issues", [])
    if not isinstance(related, list) or any(type(item) is not int or item < 1 for item in related):
        raise ValueError("invalid_related_issues")
    return {
        "id": check_id,
        "title": _prose(check["title"], 140),
        "category": check["category"],
        "expected": _prose(check["expected"]),
        "actual": _prose(check["actual"]),
        "evidence": [_prose(item) for item in evidence[:8]],
        "related_issues": list(dict.fromkeys(related)),
        "signature": check["attempts"][0]["signature"],
        "attempt_count": len(check["attempts"]),
    }


def _platform(value):
    if isinstance(value, str):
        value = value.lower()
        if value.startswith(("darwin", "macos", "mac", "osx")):
            return "macOS"
        if value.startswith("linux"):
            return "Linux"
        if value.startswith(("win", "cygwin")):
            return "Windows"
    return "未知平台"


def _body(check, report):
    commit = report.get("commit", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        commit = "未提供有效 commit"
    dirty = report.get("dirty") is True
    source_step = (
        "1. 本次验证基于下列 commit，并包含尚未提交的工作区改动；复现需要相同改动，单独检出 commit 不足以复现。按项目说明安装测试依赖。"
        if dirty else "1. 检出下列 commit，按项目说明安装测试依赖。"
    )
    evidence = "\n".join("- " + item for item in check["evidence"]) or "- 详见本地隔离测试报告。"
    return "\n".join([
        "<!-- w2f-qa:" + check["id"] + " -->",
        "本 issue 由隔离功能测试自动生成；使用合成数据和替身服务。",
        "",
        "## 实际行为",
        check["actual"],
        "",
        "## 预期行为",
        check["expected"],
        "",
        "## 复现步骤",
        source_step,
        "2. 执行对应的隔离测试：",
        "```sh",
        "python3 -m qa.run --check " + check["id"],
        "```",
        "3. 对照预期与实际行为及本地结构化测试报告；原始日志不上传至 issue。",
        "",
        "## 验证证据",
        evidence,
        "- 连续失败次数：" + str(check["attempt_count"]),
        "- 一致的失败签名：`" + check["signature"] + "`",
        "- Commit：`" + commit + "`",
        "- 工作区：" + ("包含尚未提交的改动" if dirty else "以报告所列 commit 为基准"),
        "- 平台：" + _platform(report.get("platform")),
        "- 场景 ID：`" + check["id"] + "`",
        "",
        "## 验收条件",
        "- [ ] " + check["expected"],
        "- [ ] 同一隔离场景连续执行至少两次，全部通过。",
        "- [ ] 保留覆盖此行为的自动回归测试，现有相关测试通过。",
        "",
        "## 阻塞依赖",
        "未从本次隔离验证中发现阻塞依赖；修复前应结合代码确认。",
        "",
    ])


def _gh(arguments):
    return subprocess.run(
        ["gh"] + arguments, check=True, capture_output=True, text=True, encoding="utf-8", timeout=45,
    ).stdout


def _list_issues(repo):
    # --slurp preserves page boundaries; every page must validate before any write.
    pages = json.loads(_gh([
        "api", "--paginate", "--slurp",
        "repos/" + repo + "/issues?state=all&per_page=100",
    ]))
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("invalid_issue_pages")
    issues = []
    for page in pages:
        for issue in page:
            if not isinstance(issue, dict):
                raise ValueError("invalid_issue")
            if "pull_request" in issue:
                continue
            if type(issue.get("number")) is not int or issue["number"] < 1:
                raise ValueError("invalid_issue_number")
            if issue.get("state") not in ("open", "closed"):
                raise ValueError("invalid_issue_state")
            if issue.get("body") is not None and not isinstance(issue["body"], str):
                raise ValueError("invalid_issue_body")
            issues.append({"number": issue["number"], "state": issue["state"], "body": issue.get("body") or ""})
    return issues


def _reference(repo, issue):
    return {"number": issue["number"], "url": "https://github.com/" + repo + "/issues/" + str(issue["number"])}


def publish(report, repo=DEFAULT_REPO, max_new=3, dry_run=False):
    """Publish proven failures, returning a decision for each report check.

    Existing issues are never commented on, edited, reopened, or closed. Closed
    matches need human regression review. On any uncertain GitHub operation the
    remaining publications stop; rerunning starts with a fresh deduplication read.
    Dry runs perform deduplication reads and return sanitized title/body drafts.
    """
    if not isinstance(report, dict) or report.get("schema_version") != 1 or not isinstance(report.get("checks"), list):
        raise ValueError("Expected a schema_version=1 report with checks")
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        raise ValueError("Invalid GitHub owner/repository")
    if type(max_new) is not int or max_new < 0:
        raise ValueError("max_new must be a non-negative integer")
    results = []
    candidates = []
    for check in report["checks"]:
        check_id = check.get("id") if isinstance(check, dict) else None
        if not isinstance(check_id, str) or not _CHECK_ID.fullmatch(check_id):
            check_id = "invalid_check"
        result = {"id": check_id, "status": "skipped"}
        results.append(result)
        if not _confirmed(check):
            result["reason"] = "not_a_repeatable_confirmed_failure"
            continue
        try:
            public = _public_check(check)
        except ValueError as error:
            result["reason"] = str(error)
            continue
        candidates.append((public, result))
    if not candidates:
        return results
    try:
        issues = _list_issues(repo)
    except (OSError, subprocess.SubprocessError, ValueError):
        for _, result in candidates:
            result.update(status="error", reason="github_read_failed_no_publication")
        return results
    issue_by_number = {issue["number"]: issue for issue in issues}
    created = 0
    stopped = False
    planned_ids = set()
    for check, result in candidates:
        marker = "<!-- w2f-qa:" + check["id"] + " -->"
        matches = [issue for issue in issues if marker in issue["body"]]
        opened = [issue for issue in matches if issue["state"] == "open"]
        if opened:
            result.update(status="duplicate", **_reference(repo, opened[0]))
            continue
        if matches:
            result.update(status="regression_review", reason="matching_issue_closed",
                          **_reference(repo, matches[0]))
            continue
        related = [issue_by_number[number] for number in check["related_issues"] if number in issue_by_number]
        if related:
            open_related = [issue for issue in related if issue["state"] == "open"]
            chosen = (open_related or related)[0]
            result.update(status="linked" if open_related else "regression_review",
                          reason="known_related_issue" if open_related else "related_issue_closed",
                          related_issues=[_reference(repo, issue) for issue in related],
                          **_reference(repo, chosen))
            continue
        if check["id"] in planned_ids:
            result.update(status="duplicate", reason="already_planned_in_this_report")
            continue
        if stopped:
            result.update(status="skipped", reason="publication_stopped_after_uncertain_result")
            continue
        if created >= max_new:
            result.update(status="limit_reached", reason="max_new_reached")
            continue
        title = "[自动功能测试] " + check["title"]
        body = _body(check, report)
        if dry_run:
            result.update(status="dry_run", title=title, body=body, labels=[check["category"]])
            created += 1
            planned_ids.add(check["id"])
            continue
        body_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="w2f-qa-", suffix=".md", delete=False) as handle:
                handle.write(body)
                body_path = Path(handle.name)
            output = _gh([
                "issue", "create", "--repo", repo, "--title", title,
                "--body-file", str(body_path), "--label", check["category"],
            ])
            match = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/issues/([1-9][0-9]*)\s*", output)
            if not match:
                raise ValueError("unrecognized_create_response")
            issue = {"number": int(match.group(1)), "state": "open", "body": body}
            issues.append(issue)
            issue_by_number[issue["number"]] = issue
            created += 1
            planned_ids.add(check["id"])
            result.update(status="created", **_reference(repo, issue))
        except (OSError, subprocess.SubprocessError, ValueError):
            result.update(status="error", reason="github_create_uncertain_no_retry")
            stopped = True
        finally:
            if body_path is not None:
                body_path.unlink(missing_ok=True)
    return results

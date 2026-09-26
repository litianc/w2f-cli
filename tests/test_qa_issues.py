"""The publisher must fail closed before any public side effect."""

import copy
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from qa import issues


SIGNATURE = "a" * 64
REPO = "litianc/w2f-cli"


def failure(check_id="people-unlink", **overrides):
    check = {
        "id": check_id,
        "title": "人员身份无法解绑",
        "status": "confirmed_failure",
        "category": "bug",
        "expected": "解绑后身份可关联其他人员。",
        "actual": "隔离请求返回 404，原关联仍然存在。",
        "evidence": ["两个全新临时数据库均复现相同行为。"],
        "related_issues": [],
        "consistent": True,
        "attempts": [{"status": "failed", "signature": SIGNATURE} for _ in range(2)],
    }
    check.update(overrides)
    return check


def report(*checks):
    return {"schema_version": 1, "commit": "1" * 40, "platform": "Darwin-26.0", "checks": list(checks)}


def existing(number, check_id=None, state="open", **overrides):
    item = {"number": number, "state": state, "body": "<!-- w2f-qa:" + check_id + " -->" if check_id else "已有人工 issue"}
    item.update(overrides)
    return item


class FakeGitHub:
    def __init__(self, pages=None, create_error=None, list_error=None, create_output=None):
        self.pages = [[]] if pages is None else pages
        self.create_error = create_error
        self.list_error = list_error
        self.create_output = create_output
        self.calls = []
        self.bodies = []
        self.paths = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[1] == "api":
            if self.list_error:
                raise self.list_error
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.pages))
        if args[1:3] != ["issue", "create"]:
            raise AssertionError("Unexpected GitHub mutation: " + repr(args))
        path = Path(args[args.index("--body-file") + 1])
        self.paths.append(path)
        self.bodies.append(path.read_text(encoding="utf-8"))
        if self.create_error:
            raise self.create_error
        output = self.create_output if self.create_output is not None else "https://github.com/" + REPO + "/issues/" + str(100 + len(self.bodies)) + "\n"
        return subprocess.CompletedProcess(args, 0, stdout=output)


class IssuePublisherTests(unittest.TestCase):
    def publish_with(self, data, fake=None, **options):
        fake = fake or FakeGitHub()
        with patch.object(issues.subprocess, "run", side_effect=fake):
            result = issues.publish(data, REPO, **options)
        return result, fake

    def test_creates_reviewable_issue_using_structured_argv_and_body_file(self):
        result, gh = self.publish_with(report(failure()))
        self.assertEqual(result[0]["status"], "created")
        self.assertEqual(result[0]["number"], 101)
        self.assertIn("--paginate", gh.calls[0][0])
        self.assertIn("--slurp", gh.calls[0][0])
        self.assertIn("?state=all&per_page=100", gh.calls[0][0][-1])
        args, kwargs = gh.calls[1]
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["check"])
        self.assertEqual(args[args.index("--label") + 1], "bug")
        self.assertNotIn("ready-for-agent", args)
        self.assertIn("<!-- w2f-qa:people-unlink -->", gh.bodies[0])
        self.assertIn("python3 -m qa.run --check people-unlink", gh.bodies[0])
        self.assertIn("## 验收条件", gh.bodies[0])
        self.assertIn("连续失败次数：2", gh.bodies[0])
        self.assertIn("平台：macOS", gh.bodies[0])
        self.assertTrue(all(not path.exists() for path in gh.paths))

    def test_reads_all_pages_before_deduplicating_and_ignores_pull_requests(self):
        gh = FakeGitHub(pages=[
            [existing(1, "people-unlink", pull_request={})],
            [existing(250, "people-unlink")],
        ])
        result, gh = self.publish_with(report(failure()), gh)
        self.assertEqual(result[0]["status"], "duplicate")
        self.assertEqual(result[0]["number"], 250)
        self.assertEqual(len(gh.calls), 1)

    def test_closed_marker_needs_regression_review_and_is_not_silently_swallowed(self):
        result, gh = self.publish_with(report(failure()), FakeGitHub([[existing(15, "people-unlink", "closed")]]))
        self.assertEqual(result[0]["status"], "regression_review")
        self.assertEqual(result[0]["reason"], "matching_issue_closed")
        self.assertEqual(len(gh.calls), 1)

    def test_open_marker_wins_over_closed_duplicate(self):
        pages = [[existing(15, "people-unlink", "closed"), existing(16, "people-unlink")]]
        result, _ = self.publish_with(report(failure()), FakeGitHub(pages))
        self.assertEqual(result[0]["status"], "duplicate")
        self.assertEqual(result[0]["number"], 16)

    def test_known_related_issue_links_without_comment_or_new_issue(self):
        result, gh = self.publish_with(report(failure(related_issues=[15])), FakeGitHub([[existing(15)]]))
        self.assertEqual(result[0]["status"], "linked")
        self.assertEqual(result[0]["number"], 15)
        self.assertEqual(len(gh.calls), 1)

    def test_closed_related_issue_requires_regression_review(self):
        result, _ = self.publish_with(report(failure(related_issues=[15])), FakeGitHub([[existing(15, state="closed")]]))
        self.assertEqual(result[0]["status"], "regression_review")
        self.assertEqual(result[0]["reason"], "related_issue_closed")

    def test_nonexistent_related_issue_does_not_hide_a_new_failure(self):
        result, gh = self.publish_with(report(failure(related_issues=[404])))
        self.assertEqual(result[0]["status"], "created")
        self.assertEqual(len(gh.calls), 2)

    def test_passed_flaky_and_blocked_never_access_github(self):
        checks = [failure(status=status) for status in ("passed", "flaky", "blocked", "failed")]
        result, gh = self.publish_with(report(*checks))
        self.assertEqual([item["status"] for item in result], ["skipped"] * 4)
        self.assertEqual(gh.calls, [])

    def test_invalid_confirmation_cannot_publish_even_with_confirmed_status(self):
        invalid = [
            failure(attempts=[]),
            failure(attempts=[{"status": "failed", "signature": SIGNATURE}]),
            failure(attempts=[{"status": "failed", "signature": SIGNATURE}, {"status": "passed", "signature": SIGNATURE}]),
            failure(attempts=[{"status": "failed", "signature": SIGNATURE}, {"status": "failed", "signature": "b" * 64}]),
            failure(attempts=[{"status": "failed", "signature": "same"}] * 2),
            failure(attempts=[None, None]),
            failure(consistent=False),
        ]
        result, gh = self.publish_with(report(*invalid))
        self.assertTrue(all(item["status"] == "skipped" for item in result))
        self.assertEqual(gh.calls, [])

    def test_every_attempt_must_fail_even_after_two_matching_failures(self):
        check = failure()
        check["attempts"].append({"status": "blocked", "signature": SIGNATURE})
        result, gh = self.publish_with(report(check))
        self.assertEqual(result[0]["status"], "skipped")
        self.assertEqual(gh.calls, [])

    def test_invalid_report_fields_are_not_stringified_or_sent(self):
        checks = [failure(id="../../credentials"), failure(title={"token": "private"}),
                  failure(evidence=[{"raw_log": "private"}]), failure(related_issues=["15"]),
                  failure(category="ready-for-agent")]
        result, gh = self.publish_with(report(*checks))
        self.assertTrue(all(item["status"] == "skipped" for item in result))
        self.assertEqual(gh.calls, [])
        self.assertNotIn("credentials", json.dumps(result))

    def test_list_failure_does_not_attempt_create_or_leak_stderr(self):
        error = subprocess.CalledProcessError(1, ["gh"], stderr="token=PRIVATE_VALUE")
        result, gh = self.publish_with(report(failure()), FakeGitHub(list_error=error))
        self.assertEqual(result[0]["reason"], "github_read_failed_no_publication")
        self.assertEqual(len(gh.calls), 1)
        self.assertNotIn("PRIVATE_VALUE", json.dumps(result))

    def test_malformed_page_fails_closed_before_any_publication(self):
        for pages in ([existing(10)], [[{"number": 1, "state": "unknown"}]], {"message": "bad"}):
            with self.subTest(pages=pages):
                result, gh = self.publish_with(report(failure()), FakeGitHub(pages=pages))
                self.assertEqual(result[0]["status"], "error")
                self.assertEqual(len(gh.calls), 1)

    def test_unknown_create_result_stops_batch_without_retry(self):
        checks = [failure("one"), failure("two")]
        result, gh = self.publish_with(report(*checks), FakeGitHub(create_output="unexpected stdout PRIVATE_VALUE"))
        self.assertEqual(result[0]["reason"], "github_create_uncertain_no_retry")
        self.assertEqual(result[1]["reason"], "publication_stopped_after_uncertain_result")
        self.assertEqual(len(gh.calls), 2)
        self.assertNotIn("PRIVATE_VALUE", json.dumps(result))
        self.assertTrue(all(not path.exists() for path in gh.paths))

    def test_timeout_or_command_failure_does_not_fallback_create(self):
        errors = [subprocess.TimeoutExpired(["gh"], 45), subprocess.CalledProcessError(1, ["gh"]), FileNotFoundError("gh")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                result, gh = self.publish_with(report(failure("one"), failure("two")), FakeGitHub(create_error=error))
                self.assertEqual([item["status"] for item in result], ["error", "skipped"])
                self.assertEqual(len(gh.calls), 2)

    def test_new_issue_cap_does_not_prevent_linking_existing_issues(self):
        checks = [failure("one"), failure("two"), failure("known", related_issues=[15])]
        result, gh = self.publish_with(report(*checks), FakeGitHub([[existing(15)]]), max_new=1)
        self.assertEqual([item["status"] for item in result], ["created", "limit_reached", "linked"])
        self.assertEqual(len(gh.bodies), 1)

    def test_zero_cap_has_no_create(self):
        result, gh = self.publish_with(report(failure()), max_new=0)
        self.assertEqual(result[0]["status"], "limit_reached")
        self.assertEqual(len(gh.calls), 1)

    def test_default_cap_is_three_new_issues(self):
        result, gh = self.publish_with(report(*(failure("case-" + str(index)) for index in range(4))))
        self.assertEqual([item["status"] for item in result], ["created"] * 3 + ["limit_reached"])
        self.assertEqual(len(gh.bodies), 3)

    def test_duplicate_check_in_report_only_creates_once(self):
        result, gh = self.publish_with(report(failure(), failure()))
        self.assertEqual([item["status"] for item in result], ["created", "duplicate"])
        self.assertEqual(result[0]["number"], result[1]["number"])
        self.assertEqual(len(gh.bodies), 1)

    def test_dry_run_deduplicates_and_returns_safe_capped_drafts_without_writes(self):
        checks = [failure("one"), failure("one"), failure("two"), failure("known", related_issues=[15])]
        result, gh = self.publish_with(report(*checks), FakeGitHub([[existing(15)]]), max_new=1, dry_run=True)
        self.assertEqual([item["status"] for item in result], ["dry_run", "duplicate", "limit_reached", "linked"])
        self.assertIn("body", result[0])
        self.assertEqual(len(gh.calls), 1)

    def test_report_only_uses_whitelisted_fields_and_never_raw_trace_or_environment(self):
        check = failure()
        check.update(raw_trace="PRIVATE_TRACE", environment={"PASSWORD": "PRIVATE_ENV"}, logs="PRIVATE_LOG")
        check["attempts"][0]["stdout"] = "PRIVATE_STDOUT"
        data = report(check)
        data["environment"] = {"HOME": "PRIVATE_HOME"}
        result, gh = self.publish_with(data)
        self.assertEqual(result[0]["status"], "created")
        for secret in ("PRIVATE_TRACE", "PRIVATE_ENV", "PRIVATE_LOG", "PRIVATE_STDOUT", "PRIVATE_HOME"):
            self.assertNotIn(secret, gh.bodies[0])

    def test_public_prose_redacts_secrets_paths_identities_and_markdown(self):
        check = failure(
            title="失败 @somebody ![image](https://private.test/path)",
            actual="token=super-secret-value " + "/Users/" + "qa-fixture/.credentials wxid_private oc_secret om_secret ou_secret cli_secret",
            evidence=[
                '"password": "private-password"；alice@example.test；13812345678',
                "Bearer abcdefghijk; ghp_abc12345; github_pat_secret123",
                r"C:\Users\Alice\secret.txt /home/alice/private.txt",
                "https://example.test/?token=private-token <script>alert(1)</script>",
                "chat_id=secret-chat; user_id=secret-user; app_id=secret-app",
            ],
        )
        result, gh = self.publish_with(report(check))
        self.assertEqual(result[0]["status"], "created")
        sent = gh.bodies[0] + " ".join(gh.calls[1][0])
        for secret in ("super-secret-value", "/Users/alice", "wxid_private", "oc_secret", "om_secret", "ou_secret", "cli_secret",
                       "private-password", "alice@example", "13812345678", "abcdefghijk", "ghp_abc12345", "github_pat_secret123",
                       "Alice", "/home/alice", "private-token", "<script>", "@somebody", "secret-chat", "secret-user", "secret-app"):
            self.assertNotIn(secret, sent)

    def test_raw_logs_pasted_into_summary_are_omitted(self):
        texts = ["Traceback (most recent call last): PRIVATE_DATA", "stdout: PRIVATE_DATA", "stderr=PRIVATE_DATA",
                 "export API_TOKEN=PRIVATE_DATA", "HOME=/Users/PRIVATE_DATA", "environment: PRIVATE_DATA"]
        for value in texts:
            with self.subTest(value=value):
                result, gh = self.publish_with(report(failure(actual=value)))
                self.assertEqual(result[0]["status"], "created")
                self.assertNotIn("PRIVATE_DATA", gh.bodies[0])

    def test_platform_and_commit_are_validated_instead_of_echoed(self):
        data = report(failure())
        data.update(commit="PRIVATE_COMMIT", platform="PRIVATE_HOSTNAME")
        _, gh = self.publish_with(data)
        self.assertNotIn("PRIVATE_COMMIT", gh.bodies[0])
        self.assertNotIn("PRIVATE_HOSTNAME", gh.bodies[0])
        self.assertIn("未知平台", gh.bodies[0])

    def test_dirty_worktree_is_not_claimed_reproducible_from_commit_alone(self):
        data = report(failure())
        data["dirty"] = True
        _, gh = self.publish_with(data)
        self.assertIn("单独检出 commit 不足以复现", gh.bodies[0])
        self.assertIn("工作区：包含尚未提交的改动", gh.bodies[0])
        self.assertNotIn("完整日志保留在本地", gh.bodies[0])

    def test_long_unit_check_id_is_supported(self):
        check_id = "unit.test_manager.PeopleTests.test_" + "repeated_synthetic_test_name_" * 5
        result, gh = self.publish_with(report(failure(check_id)))
        self.assertEqual(result[0]["status"], "created")
        self.assertIn("python3 -m qa.run --check " + check_id, gh.bodies[0])

    def test_redact_is_plain_text_and_does_not_modify_input_report(self):
        self.assertEqual(issues.redact("synthetic_user **value**"), "synthetic_user **value**")
        data = report(failure())
        original = copy.deepcopy(data)
        self.publish_with(data)
        self.assertEqual(data, original)

    def test_invalid_top_level_or_options_fail_before_github(self):
        with patch.object(issues.subprocess, "run") as run:
            for data in ({}, {"schema_version": 2, "checks": []}, {"schema_version": 1, "checks": None}):
                with self.assertRaises(ValueError):
                    issues.publish(data)
            for repo in ("https://github.com/private", "owner/repo; echo secret", "../owner/repo"):
                with self.assertRaises(ValueError):
                    issues.publish(report(failure()), repo)
            for cap in (-1, True, "3"):
                with self.assertRaises(ValueError):
                    issues.publish(report(failure()), max_new=cap)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

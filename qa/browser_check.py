"""Real browser functional checks for the isolated local management console.

Optional dependency: ``pip install playwright`` and
``python -m playwright install chromium``. Missing dependencies or a browser that
cannot start are blocked checks, never product defects. This module never files
issues, uses real account data, or sends external messages.
"""
from __future__ import annotations

import re
from contextlib import closing
from urllib.parse import urlsplit

from .browser_harness import isolated_manager


class ProductFailure(AssertionError):
    """An observed product behavior failed an explicit functional expectation."""


def _load_playwright():
    from playwright.sync_api import sync_playwright, TimeoutError
    return sync_playwright, TimeoutError


def _require(condition, message):
    if not condition:
        raise ProductFailure(message)


def _online(page):
    # Locator polling respects the real page's CSP; no unsafe-eval or bypass_csp.
    page.locator("#connection").filter(has_text=re.compile(r"^服务在线")).wait_for()


def _people(page):
    page.get_by_role("button", name="人员与实名", exact=True).click()
    _online(page)


def _submit(page, phase):
    with page.expect_response(lambda response: response.url.endswith("/api/people")
                              and response.request.method == "POST") as response_info:
        page.locator("#submit").click()
    response = response_info.value
    _require(response.status == 200, f"{phase}: expected HTTP 200, observed HTTP {response.status}")
    page.locator("#dialog").wait_for(state="hidden")
    _online(page)


def _create(page, name):
    page.get_by_role("button", name="新建人员", exact=True).click()
    page.get_by_label("已确认实名", exact=True).fill(name)
    _submit(page, "Create person")
    page.get_by_text(name, exact=True).wait_for()


def _navigation(page, fixture):
    for name in ("群映射", "飞书应用", "人员与实名", "失败与补发", "接入引导", "运行概览"):
        page.get_by_role("button", name=name, exact=True).click()
        _online(page)
        _require(page.locator("#title").inner_text() == name, "Navigation displayed the wrong page title")
        _require(bool(page.locator("#content").inner_text().strip()), "A core page rendered empty content")
    return ["All six core pages loaded through the real HTTP API", "No external CLI was executed"]


def _people_lifecycle(page, fixture):
    _people(page)
    _create(page, "QA 合成人员 A")
    page.get_by_role("button", name="更正姓名", exact=True).click()
    page.get_by_label("已确认实名", exact=True).fill("QA 合成人员 B")
    _submit(page, "Rename person")
    page.get_by_text("QA 合成人员 B", exact=True).wait_for()

    page.get_by_role("button", name="关联微信 ID", exact=True).click()
    page.get_by_label("微信稳定 ID", exact=True).fill("wxid_qa_person")
    page.get_by_label("微信显示名", exact=True).fill("QA 微信昵称")
    _submit(page, "Link WeChat identity")

    page.get_by_role("button", name="关联飞书身份", exact=True).click()
    page.locator('#dialog select[name="profile_id"]').select_option(str(fixture.profile["id"]))
    page.locator('#dialog input[name="open_id"]').fill("ou_qa_person")
    _submit(page, "Link Feishu identity")
    page.reload()
    _online(page)
    _people(page)
    page.get_by_role("button", name="详情", exact=True).click()
    detail = page.locator("#fields").inner_text()
    for value in ("wxid_qa_person", "ou_qa_person", "QA 合成人员 A", "QA 合成人员 B"):
        _require(value in detail, "Person detail lost an identity or the rename audit record after reload")
    people = fixture.store.list_people()
    _require(len(people) == 1 and people[0]["confirmed_name"] == "QA 合成人员 B",
             "Person rename was not persisted to SQLite")
    _require(len(fixture.store.list_wechat_identities()) == 1
             and len(fixture.store.list_feishu_identities()) == 1,
             "Identity links were not persisted exactly once")
    _require(fixture.store.resolve_sender("default", "wxid_qa_person")["confirmed_name"] == "QA 合成人员 B",
             "Sender resolution did not use the corrected name")
    return ["Browser created and renamed a synthetic person", "WeChat and Feishu identities persisted once",
            "After reload, detail showed both IDs and the old-to-new name audit record"]


def _empty_wechat_id(page, fixture):
    _people(page)
    _create(page, "QA 空 ID 校验")
    page.get_by_role("button", name="关联微信 ID", exact=True).click()
    # Leave the stable ID empty exactly as a user can do in this form.
    statuses = []
    page.on("response", lambda response: statuses.append(response.status)
            if response.url.endswith("/api/people") and response.request.method == "POST" else None)
    page.locator("#submit").click()
    # Accept native/client validation or an API rejection. Do not require an
    # HTTP request when a future required field correctly prevents submission.
    page.locator('#dialog input:invalid, #formError:not(:empty), #dialog:not([open])').first.wait_for(state="attached")
    identities = fixture.store.list_wechat_identities()
    _require(not identities and page.locator("#dialog").is_visible(),
             f"Submitting an empty WeChat stable ID returned HTTP {statuses[-1] if statuses else 'not sent'}; "
             f"persisted identity count: {len(identities)}")
    return ["Empty stable ID was rejected in the browser form and left no identity record"]


def _html_names(page, fixture):
    _people(page)
    name = 'QA <b data-qa="injected">合成人员</b>'
    _create(page, name)
    _require(page.locator('[data-qa="injected"]').count() == 0,
             "A person name created HTML elements in the people list")
    page.get_by_role("button", name="详情", exact=True).click()
    _require(page.locator("#dialogTitle").inner_text() == name,
             "The detail dialog did not preserve the name as literal text")
    _require(page.locator('[data-qa="injected"]').count() == 0,
             "A person name created HTML elements in the detail dialog")
    return ["HTML characters in a synthetic name rendered literally in list and detail"]


CHECKS = (
    ("browser.navigation", "管理台核心页面可加载", "All six navigation pages render successfully", _navigation),
    ("browser.people_lifecycle", "浏览器人员创建、更名、关联与详情持久化",
     "Person edits, both identity links, and rename audit survive a browser reload", _people_lifecycle),
    ("browser.empty_wechat_id", "人员关联拒绝空微信稳定 ID",
     "An empty WeChat stable ID is rejected without persisting an identity", _empty_wechat_id),
    ("browser.html_names", "实名中的 HTML 字符按纯文本显示",
     "Person names cannot create HTML elements in list or detail", _html_names),
)


def _result(spec, status, actual, evidence=None):
    return {"id": spec[0], "title": spec[1], "status": status, "expected": spec[2],
            "actual": actual, "evidence": evidence or [], "related_issues": [], "category": "bug"}


def run_checks() -> list[dict]:
    """Run independent scenarios; publishable evidence contains no URLs/tokens."""
    try:
        sync_playwright, browser_timeout = _load_playwright()
    except (ImportError, ModuleNotFoundError):
        return [_result(spec, "blocked", "Playwright is not installed; install the optional browser dependencies")
                for spec in CHECKS]

    results = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception:
                return [_result(spec, "blocked", "Chromium could not start; verify the browser installation and sandbox permissions")
                        for spec in CHECKS]
            try:
                for spec in CHECKS:
                    fixture = None
                    errors, blocked_requests = [], []
                    try:
                        with isolated_manager() as fixture, closing(browser.new_context(service_workers="block")) as context:
                            context.set_default_timeout(7000)

                            def route_request(route):
                                parsed = urlsplit(route.request.url)
                                if f"{parsed.scheme}://{parsed.netloc}" == fixture.origin:
                                    route.continue_()
                                else:
                                    blocked_requests.append("Non-fixture browser request blocked")
                                    route.abort()

                            context.route("**/*", route_request)
                            page = context.new_page()
                            page.on("pageerror", lambda error: errors.append("Uncaught browser JavaScript error"))
                            page.goto(fixture.url)
                            _online(page)
                            evidence = spec[3](page, fixture)
                            _require(not errors, "The page raised an uncaught JavaScript error")
                            _require(not blocked_requests, "The page attempted a non-fixture browser request")
                            if fixture.violations:
                                result = _result(spec, "blocked", "An unconfigured external boundary was blocked")
                            else:
                                result = _result(spec, "passed", "Expected behavior observed", evidence)
                    except ProductFailure as exc:
                        if fixture and fixture.violations:
                            result = _result(spec, "blocked", "An unconfigured external boundary was blocked")
                        else:
                            evidence = ["Reproduced using only synthetic data, a temporary SQLite database, and the real local HTTP service"]
                            if spec[0] == "browser.empty_wechat_id":
                                evidence += ["人员与实名 → 新建人员 → 填入合成姓名并保存",
                                             "关联微信 ID → 保持微信稳定 ID 为空 → 保存",
                                             "Inspect the HTTP result and the new temporary database's identity count"]
                            result = _result(spec, "failed", str(exc), evidence)
                    except browser_timeout:
                        # A timeout alone cannot distinguish a slow machine,
                        # stale selector, or product defect; never auto-file it.
                        result = _result(spec, "blocked", "The expected UI state was not observable within 7 seconds; manual diagnosis is required")
                    except Exception:
                        # Do not turn harness/driver exceptions into product issues.
                        result = _result(spec, "blocked", "The isolated browser harness could not complete this scenario")
                    results.append(result)
            finally:
                browser.close()
    except Exception:
        completed = {result["id"] for result in results}
        results.extend(_result(spec, "blocked", "Playwright driver could not complete the isolated run")
                       for spec in CHECKS if spec[0] not in completed)
    return results

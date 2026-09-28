"""Wizard acceptance uses the real HTTP/UI and synthetic external CLI boundaries."""
def expect(locator):
    from playwright.sync_api import expect as playwright_expect
    return playwright_expect(locator)


def _start(page, fixture, *, people=False):
    from .browser_check import _online
    f = fixture.wizard
    f.enabled = True
    f.sources = [
        {'username': 'qa-source-a', 'display_name': '项目 😀 <b>讨论</b> "群"'},
        {'username': 'qa-source-b', 'display_name': '第二个项目群'},
        {'username': 'qa-source-long', 'display_name': '长' * 61},
        {'username': 'qa-source-empty', 'display_name': ''},
    ]
    other = fixture.store.upsert_profile('qa-other', 'cli_qa_other')
    if people:
        for uid, name, profile in [('ou_qa_first', '同名 😀', fixture.profile),
                                   ('ou_qa_second', '同名 😀', fixture.profile),
                                   ('ou_qa_third', '另一人', fixture.profile),
                                   ('ou_qa_foreign', '异应用人员', other)]:
            person = fixture.store.create_person(name)
            fixture.store.link_feishu_identity(person['id'], profile['id'], uid)
    page.get_by_role('button', name='群映射', exact=True).click()
    _online(page)
    page.get_by_role('button', name='新建群映射', exact=True).click()
    _online(page)
    page.locator('[name="wz-profile"]').select_option('qa-browser')
    page.get_by_role('button', name='新建群', exact=True).click()


def _source(page, index):
    key = ['qa-source-a', 'qa-source-b', 'qa-source-long', 'qa-source-empty'][index]
    page.locator('[data-action="wz-pick-source"]').filter(has_text=key).click()
    page.get_by_text('✓ 可用', exact=True).first.wait_for()


def _create(page):
    with page.expect_response(lambda r: r.url.endswith('/api/wizard') and r.request.method == 'POST') as response:
        page.get_by_role('button', name='创建并选中', exact=True).click()
    return response.value.json()


def name_fill(page, fixture):
    from .browser_check import _require
    _start(page, fixture)
    fill = page.get_by_role('button', name='使用微信群名', exact=True)
    name = page.get_by_label('新群名称（最长 60 字）', exact=True)
    expect(fill).to_be_disabled()
    _source(page, 0)
    fill.focus();page.keyboard.press('Space')
    expect(name).to_have_value(fixture.wizard.sources[0]['display_name'])
    expect(name).to_be_focused()
    _require(name.evaluate('(e)=>e.selectionStart===e.value.length && e.selectionEnd===e.value.length'), 'Copy must leave the cursor at the end')
    _require(page.locator('#content b b').count() == 0, 'Group name was interpreted as HTML')
    name.fill('自定义 😀 "名称"')
    _source(page, 1)
    page.get_by_role('button', name='选择已有群', exact=True).click()
    page.get_by_role('button', name='新建群', exact=True).click()
    page.locator('[name="wz-profile"]').select_option('qa-other')
    expect(name).to_have_value('自定义 😀 "名称"')
    page.locator('[name="wz-profile"]').select_option('qa-browser')
    fill.click();expect(name).to_have_value('第二个项目群')
    _source(page, 2);fill.click();expect(name).to_have_value('长' * 61)
    page.get_by_text('名称超过 60 字，请编辑后再创建（不会自动截断）。', exact=True).wait_for()
    page.get_by_role('button', name='创建并选中', exact=True).click()
    _require(not fixture.wizard.creates, 'Copy or overlong name created a remote group')
    _source(page, 3);expect(fill).to_be_disabled()
    _source(page, 0);fill.click()
    for width in (1280, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        a, b = fill.bounding_box(), name.bounding_box()
        _require(a['x'] >= 0 and a['x'] + a['width'] <= width and b['x'] + b['width'] <= width,
                 'Name field or button overflows the viewport')
        _require(a['y'] + a['height'] <= b['y'] + 1, 'Name button overlaps the input')
    name.fill('编辑后的新群 😀')
    result = _create(page)
    _require(result.get('chat_id') == 'oc_qa_created_1', 'Created group was not returned')
    args = fixture.wizard.creates[0]
    _require(args[args.index('--name')+1] == '编辑后的新群 😀' and '--users' not in args,
             'Final edited name or zero-member request was wrong')
    page.get_by_label('群邀请链接').wait_for()
    page.get_by_role('button', name='预检并创建', exact=True).wait_for()
    return ['Source copy preserved literal Unicode/HTML, cursor and custom edits',
            'No copy-triggered creation; 61 characters rejected without truncation',
            'Desktop and 390px layout fit; actual HTTP submitted the final edited name with zero members']


def member_selection(page, fixture):
    from .browser_check import _require
    _start(page, fixture, people=True);_source(page, 0)
    page.get_by_role('button', name='使用微信群名', exact=True).click()
    search = page.get_by_label('搜索初始成员姓名')
    _require(page.locator('[name="wz-create-member"]').count() == 3, 'Candidates include another application')
    search.fill('同名')
    page.locator('[value="ou_qa_first"]').check()
    page.locator('[value="ou_qa_second"]').check()
    expect(page.locator('#wzMemberCount')).to_have_text('已选 2 人')
    search.fill('另一人');page.locator('[value="ou_qa_third"]').check()
    expect(page.locator('#wzMemberCount')).to_have_text('已选 3 人')
    search.fill('同名');page.locator('[value="ou_qa_first"]').uncheck()
    expect(page.locator('#wzMemberCount')).to_have_text('已选 2 人')
    page.get_by_role('button', name='清空', exact=True).click()
    expect(page.locator('#wzMemberCount')).to_have_text('已选 0 人')
    page.locator('[value="ou_qa_second"]').check()
    page.locator('[name="wz-profile"]').select_option('qa-other')
    expect(page.locator('#wzMemberCount')).to_have_text('已选 0 人')
    _require(page.locator('[name="wz-create-member"]').count() == 1, 'Profile switch did not refresh candidates')
    expect(page.get_by_label('新群名称（最长 60 字）', exact=True)).to_have_value(fixture.wizard.sources[0]['display_name'])
    page.locator('[name="wz-profile"]').select_option('qa-browser')
    page.locator('[value="ou_qa_first"]').check();page.locator('[value="ou_qa_second"]').check()
    _require(not fixture.wizard.creates, 'Selecting members caused a write')
    result = _create(page)
    _require(result['members']['joined'] == ['ou_qa_first', 'ou_qa_second'], 'Multiple selected members were not all verified')
    page.get_by_text('群已创建 · 初始成员已核验', exact=True).wait_for()
    page.get_by_label('群邀请链接').wait_for()
    page.get_by_role('button', name='新建群', exact=True).click()
    page.get_by_role('button', name='使用微信群名', exact=True).click()
    page.locator('[value="ou_qa_third"]').check()
    result = _create(page)
    _require(result['members']['joined'] == ['ou_qa_third'], 'Single-member creation regressed')
    return ['Search/select/unselect/clear preserve IDs and same-name people',
            'Profile switch cleared selection but preserved name', 'Real HTTP submitted and verified two members, then one member']


def creation_results(page, fixture):
    from .browser_check import _require, _online
    from agents.chat_creation import CliRequestError
    _start(page, fixture, people=True);_source(page, 0)
    name = page.get_by_label('新群名称（最长 60 字）', exact=True)
    name.fill('部分成员测试')
    page.locator('[value="ou_qa_first"]').check();page.locator('[value="ou_qa_second"]').check()
    fixture.wizard.error = CliRequestError('合成权限失败', uncertain=False)
    _create(page)
    expect(name).to_have_value('部分成员测试')
    expect(page.locator('#wzMemberCount')).to_have_text('已选 2 人')
    fixture.wizard.error = None;fixture.wizard.members = ['ou_qa_first']
    result = _create(page)
    _require(result['members']['missing'] == ['ou_qa_second'], 'Partial member result was lost')
    page.get_by_text('群已创建 · 部分成员未在群中', exact=True).wait_for()
    page.get_by_label('群邀请链接').wait_for()
    _require(len(fixture.wizard.targets) == 1, 'Member failure duplicated a group')
    page.get_by_role('button', name='新建群', exact=True).click()
    name.fill('超时测试')
    fixture.wizard.error = CliRequestError('合成超时')
    _create(page)
    expect(page.get_by_role('button', name='创建并选中', exact=True)).to_be_disabled()
    count = len(fixture.wizard.creates)
    page.reload();_online(page)
    page.get_by_role('button', name='群映射', exact=True).click();_online(page)
    page.get_by_role('button', name='新建群映射', exact=True).click();_online(page)
    expect(name).to_have_value('超时测试')
    with page.expect_response(lambda r: r.url.endswith('/api/wizard') and r.request.method == 'POST'):
        page.get_by_role('button', name='检查本次请求', exact=True).click()
    _require(len(fixture.wizard.creates) == count, 'Uncertain request was resent after page reload')
    return ['Definite failure preserved both drafts; retry created once',
            'Partial success retained chat and invitation with explicit missing member',
            'Uncertain result survived browser reload and repeated checking without another CLI create']


def busy_guard(page, fixture):
    from .browser_check import _require
    _start(page, fixture, people=True);_source(page, 0)
    page.get_by_role('button', name='使用微信群名', exact=True).click()
    fixture.wizard.release.clear()
    try:
        page.get_by_role('button', name='创建并选中', exact=True).click()
        _require(fixture.wizard.entered.wait(2), 'Synthetic create did not reach CLI boundary')
        for button in ('使用微信群名', '创建中…', '选择已有群'):
            expect(page.get_by_role('button', name=button, exact=True)).to_be_disabled()
        expect(page.locator('[name="wz-profile"]')).to_be_disabled()
        page.locator('[data-action="wz-create-group"]').dispatch_event('click')
        _require(len(fixture.wizard.creates) == 1, 'Double click created another request')
    finally:
        fixture.wizard.release.set()
    page.get_by_label('群邀请链接').wait_for()
    return ['Pending request disables name copy, profile switch and create', 'Repeated click reached the fake CLI exactly once']

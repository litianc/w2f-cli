"""Merged-forward links at the parsing and actual delivery boundaries."""
import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from tests.test_recovery import History, Sink


ARTICLE_URL = 'https://example.test/article?first=1&second=%26amp%3Bvalue#section'
ARTICLE_ITEM = (
    '<dataitem datatype="5"><sourcename>同事甲</sourcename>'
    '<link>https://example.test/legacy-link</link>'
    '<sourceheadurl>https://cdn.example.test/avatar?token=private</sourceheadurl>'
    '<weburlitem><title>文章 A &amp; B 😀</title><desc>摘要 &amp; 说明</desc>'
    '<url>' + html.escape(ARTICLE_URL) + '</url>'
    '<appmsgshareitem><coverpicimageurl>https://cdn.example.test/cover?token=private</coverpicimageurl>'
    '</appmsgshareitem></weburlitem></dataitem>'
)


def forward_xml(items):
    # Real type-19 messages enclose a second XML document in recorditem.
    return ('<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist>'
            + items + '</datalist></recordinfo>]]></recorditem></appmsg></msg>')


class ForwardLinksTest(unittest.TestCase):
    def test_native_weburlitem_preserves_title_description_and_original_link(self):
        raw = forward_xml(ARTICLE_ITEM)
        self.assertEqual(
            mirror.clean_body({'kind_name': 'forward_chat', 'text': raw}),
            '同事甲：文章 A & B 😀\n摘要 & 说明\n' + ARTICLE_URL,
        )

    def test_link_fields_fall_back_without_forwarding_invalid_or_auxiliary_urls(self):
        cases = [
            ('<weburlitem><title>标题</title><url>javascript:alert(1)</url></weburlitem>'
             '<link>https://example.test/fallback</link>', '标题\nhttps://example.test/fallback'),
            ('<weburlitem><title>标题</title><url>https://</url></weburlitem>', '标题'),
            ('<weburlitem><url>https://example.test:invalid/path</url></weburlitem>', '[原消息内容不可用]'),
            ('<weburlitem><url>https://credentials@example.test/path</url></weburlitem>', '[原消息内容不可用]'),
            ('<weburlitem><url>https://example.test/\nnext</url></weburlitem>', '[原消息内容不可用]'),
            ('<sourceheadurl>https://cdn.example.test/avatar?token=private</sourceheadurl>'
             '<weburlitem><appmsgshareitem><url>https://cdn.example.test/cover?token=private</url>'
             '</appmsgshareitem></weburlitem>', '[原消息内容不可用]'),
            ('<datatitle>旧格式标题</datatitle><datadesc>旧格式摘要</datadesc>'
             '<weburl>https://example.test/legacy</weburl>', '旧格式标题\n旧格式摘要\nhttps://example.test/legacy'),
            ("<datatitle>标题</datatitle><weburl>https://example.test/reader&apos;s-guide</weburl>",
             "标题\nhttps://example.test/reader's-guide"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                raw = forward_xml('<dataitem datatype="5"><sourcename>甲</sourcename>' + fields + '</dataitem>')
                self.assertEqual(mirror.clean_body({'kind_name': 'forward_chat', 'text': raw}), '甲：' + expected)

    def test_nested_escaped_record_and_cdata_keep_literal_url_entities(self):
        url = 'https://example.test/article?a=1&amp;literal=2&encoded=%26amp%3B'
        inner = ('<datalist><dataitem datatype="5"><sourcename>内层</sourcename>'
                 '<weburlitem><title>标题</title><desc>标题</desc><url><![CDATA['
                 + url + ']]></url></weburlitem></dataitem></datalist>')
        raw = forward_xml('<dataitem datatype="17"><sourcename>外层</sourcename>'
                          '<recorditem>' + html.escape(inner) + '</recorditem></dataitem>')
        self.assertEqual(mirror.forward_body({'kind_name': 'forward_chat', 'text': raw}, {}, None),
                         '外层：[原消息内容不可用]\n  内层：标题\n' + url)

    def test_literal_angle_bracket_title_and_apostrophe_url_are_not_lost(self):
        cases = [
            ('标题', "https://example.test/articles/O'Reilly?tag=it's"),
            ('<AI> news', 'https://example.test/article'),
        ]
        for title, url in cases:
            with self.subTest(title=title):
                raw = forward_xml('<dataitem datatype="5"><sourcename>甲</sourcename>'
                                  '<weburlitem><title>' + html.escape(title) + '</title><desc>摘要</desc>'
                                  '<url>' + html.escape(url) + '</url></weburlitem></dataitem>')
                self.assertEqual(mirror.clean_body({'kind_name': 'forward_chat', 'text': raw}),
                                 '甲：' + title + '\n摘要\n' + url)

    def test_legacy_raw_xml_is_suppressed_without_losing_explicit_article_fields(self):
        xml = '<msg><sourceheadurl>https://cdn.example.test/avatar?token=private</sourceheadurl></msg>'
        for tag in ('content', 'datadesc', 'des'):
            for value in (xml, xml[:-6]):
                with self.subTest(tag=tag, complete=value==xml):
                    fallback = '<' + tag + '>' + html.escape(value) + '</' + tag + '>'
                    raw = forward_xml('<dataitem datatype="5"><sourcename>甲</sourcename>'
                                      + fallback + '</dataitem>')
                    self.assertEqual(mirror.clean_body({'kind_name': 'forward_chat', 'text': raw}),
                                     '甲：[原消息内容不可用]')
                    raw = forward_xml('<dataitem datatype="5"><sourcename>甲</sourcename>' + fallback
                                      + '<weburlitem><title>&lt;AI&gt; news</title>'
                                      '<url>https://example.test/article</url></weburlitem></dataitem>')
                    self.assertEqual(mirror.clean_body({'kind_name': 'forward_chat', 'text': raw}),
                                     '甲：<AI> news\nhttps://example.test/article')


class ForwardLinkDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MirrorStore(Path(self.tmp.name) / 'state.db')
        self.config = {'binding_id': 1, 'source_account': 'demo', 'wechat_talker': 'room',
                       'feishu_chat_id': 'chat', 'lark_profile': 'fixture', 'lark_app_id': 'app',
                       'initial_cursor': 10, 'sender_map': {'outer': '外层实名', 'inner': '内层实名'}}
        self.store.initialize(self.config)
        self.store.set_control(1, 'running')
        self.sink = Sink()
        sender = patch.object(mirror, 'run_lark', self.sink)
        sender.start()
        self.addCleanup(sender.stop)

    def row(self, local_id, raw, kind='forward_chat'):
        return {'talker': 'room', 'local_id': local_id, 'server_id': str(local_id),
                'sender_wxid': 'outer', 'sender': '外层昵称', 'create_time': 1000 + local_id,
                'kind_name': kind, 'text': raw}

    def texts(self):
        return [args[args.index('--text') + 1] for _, args in self.sink.sent]

    def test_article_text_and_media_placeholder_keep_order_names_and_identity_isolation(self):
        text = ('<dataitem datatype="1"><sourcename>内层昵称</sourcename>'
                '<dataitemsource><realchatname>inner</realchatname></dataitemsource>'
                '<datadesc>前文 😀</datadesc></dataitem>')
        image = '<dataitem datatype="2"><sourcename>同事乙</sourcename></dataitem>'
        rows = [self.row(11, forward_xml(text + ARTICLE_ITEM + image)),
                self.row(12, '后续文字', 'text')]
        identities = self.store.list_wechat_identities()
        self.assertEqual(mirror.recover_once(self.config, self.store, History(rows)), 2)
        self.assertEqual(self.texts(), [
            '外层实名转发聊天记录（第 1/1 段）\n内层实名：前文 😀\n'
            '同事甲：文章 A & B 😀\n摘要 & 说明\n' + ARTICLE_URL + '\n同事乙：[图片]',
            '同事乙：（转发记录第 3 条图片）[图片资源不可用，待人工排查]',
            '外层实名：后续文字',
        ])
        self.assertEqual(self.store.list_wechat_identities(), identities)
        self.assertEqual(self.store.list_confirmations(), [])
        self.assertEqual(self.store.get_cursor(), 12)
        self.assertEqual(len(self.store.list_media_failures()),1)

    def test_lost_receipt_restart_reuses_rendered_article_and_does_not_duplicate(self):
        source = History([self.row(11, forward_xml(ARTICLE_ITEM))])
        self.sink.timeout_after = 1
        with self.assertRaises(mirror.CliError):
            mirror.recover_once(self.config, self.store, source)
        accepted = self.texts()
        restarted = MirrorStore(self.store.path)
        with patch.object(mirror, 'forward_record_texts', side_effect=AssertionError('must reuse saved plan')):
            mirror.recover_once(self.config, restarted, source)
            self.assertEqual(mirror.recover_once(self.config, restarted, source), 0)
        self.assertEqual(self.texts(), accepted)
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(restarted.get_cursor(), 11)

    def test_upgrade_preserves_old_plan_and_sent_history_while_new_messages_use_fix(self):
        old_row = self.row(11, forward_xml(ARTICLE_ITEM))
        with patch.object(mirror, 'forward_record_texts', return_value=['同事甲：[原消息内容不可用]']):
            with patch.object(mirror, 'run_lark', side_effect=mirror.CliError('synthetic refusal')):
                with self.assertRaises(mirror.CliError):
                    mirror.send_row(self.config, old_row, {}, self.store)
        restarted = MirrorStore(self.store.path)
        mirror.send_row(self.config, old_row, {}, restarted)
        mirror.send_row(self.config, old_row, {}, restarted)
        mirror.send_row(self.config, self.row(12, forward_xml(ARTICLE_ITEM)), {}, restarted)
        self.assertEqual(self.texts(), [
            '外层实名转发聊天记录（第 1/1 段）\n同事甲：[原消息内容不可用]',
            '外层实名转发聊天记录（第 1/1 段）\n同事甲：文章 A & B 😀\n摘要 & 说明\n' + ARTICLE_URL,
        ])


if __name__ == '__main__':
    unittest.main()

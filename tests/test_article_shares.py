"""Article shares retain exact links and cover delivery across retries."""
import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.wda_reader import map_row


TITLE = '中文 😀 & 项目进展'
SHORT = 'https://mp.weixin.qq.com/s/example'
LONG = 'https://mp.weixin.qq.com/s?__biz=example&mid=123&idx=1&sn=abc&query=%E4%B8%AD%E6%96%87%26x%3Dy'
COVER = 'https://example.com/cover.jpg?size=large&v=2'


def article_xml(url=LONG, cdata=False, cover=COVER):
    field = (lambda s: '<![CDATA[' + s + ']]>') if cdata else html.escape
    return '<msg><appmsg><type>5</type><title>' + field(TITLE) + '</title><url>' + field(url) + '</url><des>摘要</des><thumburl>' + field(cover) + '</thumburl><avatar>https://example.com/avatar.jpg</avatar></appmsg></msg>'


class ArticleSharesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.store = MirrorStore(Path(tmp.name) / 'state.db')
        self.config = {'wechat_talker': 'room', 'feishu_chat_id': 'chat', 'lark_profile': 'fixture', 'sender_map': {'wx': '实名'}}
        self.store.initialize(self.config)
        self.calls = []
    def row(self, raw, identity=1):
        return {'talker': 'room', 'local_id': identity, 'sender_wxid': 'wx', 'kind_name': 'link', 'text': raw}
    def transport(self, config, args):
        self.calls.append(args)
        return {'data': {'message_id': 'om_fixture_' + str(len(self.calls))}}

    def test_xml_entities_and_cdata_keep_exact_article_url_only(self):
        for url in (SHORT, LONG):
            for cdata in (False, True):
                with self.subTest(url=url, cdata=cdata):
                    body = mirror.clean_body(self.row(article_xml(url, cdata)))
                    self.assertIn(TITLE, body)
                    self.assertIn(url, body.splitlines())
                    self.assertNotIn('CDATA', body)
                    self.assertNotIn('&amp;', body)
                    self.assertNotIn(']]', body)
                    self.assertNotIn('cover.jpg', body)
                    self.assertNotIn('avatar.jpg', body)

    def test_short_and_long_links_send_title_link_and_explicit_cover(self):
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            for index, url in enumerate((SHORT, LONG), 1):
                mirror.send_row(self.config, self.row(article_xml(url), index), {}, self.store)
        texts = [a[a.index('--text') + 1] for a in self.calls if '--text' in a]
        covers = [a[a.index('--image') + 1] for a in self.calls if '--image' in a]
        self.assertEqual(len(texts), 2)
        for text, url in zip(texts, (SHORT, LONG)):
            self.assertIn('实名：' + TITLE, text)
            self.assertIn(url, text.splitlines())
        self.assertEqual(covers, [COVER, COVER])

    def test_wda_keeps_metadata_and_persists_for_retry(self):
        row = map_row('room', {'type': (5 << 32) | 49, 'localId': 4, 'senderUsername': 'wx',
            'title': TITLE, 'url': LONG, 'thumbUrl': COVER, 'description': '摘要'}, {})
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            mirror.send_row(self.config, row, {}, self.store)
        self.assertTrue(any('--image' in a and COVER in a for a in self.calls))
        saved = json.loads(self.store.message('room:4', 1)['payload_json'])
        self.assertEqual(saved['article']['url'], LONG)
        self.assertEqual(saved['article']['cover_url'], COVER)

    def test_pasted_url_no_metadata_does_not_download_or_modify_parameters(self):
        row = self.row(LONG)
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            mirror.send_row(self.config, row, {}, self.store)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][self.calls[0].index('--text') + 1], '实名：' + LONG)

    def test_missing_cover_sends_readable_text_without_media_job(self):
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            mirror.send_row(self.config, self.row(article_xml(cover='')), {}, self.store)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.message('room:1', 1)['state'], 'sent')
        self.assertEqual(self.store.list_media_failures(), [])

    def test_definite_cover_failure_allows_next_message_and_manual_retry_no_duplicate_text(self):
        row = self.row(article_xml())
        def reject(config, args):
            if '--image' in args: raise mirror.CliError('cover unavailable')
            return self.transport(config, args)
        with patch.object(mirror, 'run_lark', side_effect=reject):
            mirror.send_row(self.config, row, {}, self.store)
            mirror.send_row(self.config, self.row('next', 2), {}, self.store)
        self.assertEqual(self.store.message('room:1', 1)['state'], 'placeholder')
        self.assertEqual(self.store.message('room:2', 1)['state'], 'sent')
        self.assertEqual(len(self.store.list_media_failures()), 1)
        self.store = MirrorStore(self.store.path)
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            saved = json.loads(self.store.message('room:1', 1)['payload_json'])
            mirror.send_row({**self.config, '_retry_media': True}, saved, {}, self.store)
            mirror.send_row(self.config, saved, {}, self.store)
        self.assertEqual(len([a for a in self.calls if '--text' in a]), 2)
        self.assertEqual(len([a for a in self.calls if '--image' in a]), 1)
        self.assertEqual(self.store.list_media_failures(), [])

    def test_uncertain_cover_receipt_is_not_reported_as_success(self):
        def uncertain(config, args):
            if '--image' in args: raise mirror.CliError('timeout', uncertain=True)
            return self.transport(config, args)
        with patch.object(mirror, 'run_lark', side_effect=uncertain):
            mirror.send_row(self.config, self.row(article_xml()), {}, self.store)
            mirror.send_row(self.config, self.row('next', 2), {}, self.store)
        message = self.store.message('room:1', 1)
        self.assertEqual(message['state'], 'placeholder')
        self.assertEqual(self.store.message('room:2', 1)['state'], 'sent')
        self.assertEqual(self.store.delivery(message['id'], 'article-cover')['state'], 'uncertain')
        self.assertEqual(self.store.list_media_failures()[0]['state'], 'manual')
        # Beyond Feishu's UUID window, manual retries must not blindly write.
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET attempted_at=1 WHERE message_id=? AND part=?', (message['id'], 'article-cover'))
        with patch.object(mirror, 'run_lark', side_effect=AssertionError('uncertain cover must be checked first')):
            mirror.send_row({**self.config, '_retry_media': True}, self.row(article_xml()), {}, self.store)
        self.assertEqual(self.store.delivery(message['id'], 'article-cover')['state'], 'uncertain')

    def test_wda_raw_xml_fallback_and_plain_web_link(self):
        row = map_row('room', {'type': (5 << 32) | 49, 'localId': 7, 'content': article_xml(cdata=True)}, {})
        self.assertEqual(row['article']['url'], LONG)
        self.assertEqual(row['article']['cover_url'], COVER)
        self.assertEqual(mirror.clean_body(self.row('https://example.com/?x=1&y=2')), 'https://example.com/?x=1&y=2')

    def test_completed_cover_receipt_survives_worker_restart(self):
        row = self.row(article_xml())
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            mirror.send_row(self.config, row, {}, self.store)
            self.store.mark_message('room:1', 'processing', binding_id=1)
            self.store = MirrorStore(self.store.path)
            mirror.send_row(self.config, row, {}, self.store)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.store.message('room:1', 1)['state'], 'sent')

    def test_non_http_cover_is_not_uploaded_and_query_is_not_double_decoded(self):
        url = 'https://example.com/?literal=one&amp;two=three&encoded=%26amp%3B'
        row = self.row(article_xml(url, cover='file:///private/cover.jpg'))
        with patch.object(mirror, 'run_lark', side_effect=self.transport):
            mirror.send_row(self.config, row, {}, self.store)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(url, self.calls[0][self.calls[0].index('--text') + 1])

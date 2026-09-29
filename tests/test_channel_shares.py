"""Channels links are parsed at the real rendering and delivery boundaries."""
import html
import base64
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.wda_reader import map_row
from tests.test_recovery import History, Sink


SHARE = 'https://weixin.qq.com/sph/synthetic-share?scene=1&detail=%E4%B8%AD%26x%3Dy#wechat_redirect'
IDS = ('<finderFeed><objectId>14954560208914487678</objectId>'
       '<objectNonceId>5397107284542310304_0_140_0_0</objectNonceId></finderFeed>')
UNAVAILABLE = '[视频号链接暂不可用，待人工排查]'


class ChannelSharesTest(unittest.TestCase):
    def test_original_share_wins_over_ids_with_cdata_or_entities(self):
        for field in (html.escape(SHARE), '<![CDATA[' + SHARE + ']]>'):
            with self.subTest(field=field):
                raw = '<msg><appmsg><type>51</type><url>' + field + '</url>' + IDS + '</appmsg></msg>'
                body = mirror.clean_body({'kind_name': 'channel_video', 'text': raw})
                self.assertEqual(body, '视频号\n' + SHARE)
                self.assertNotIn(']]', body)

    def test_page_parameters_match_reference_vectors_and_decode_losslessly(self):
        # Literal vectors from wx_channels_download util_test.go at
        # 124f044235bc706fe796c043a14ff442a150ae5d, not a playback assertion.
        vectors = [('0', 'AAAAAAAAAAA'), ('1', 'AAAAAAAAAAE'), ('256', 'AAAAAAAAAQA'),
                   ('5397107284542310304', 'SuZgeu3RR6A'),
                   ('14887697630331082851', 'zpu6HctPGGM'),
                   ('14954560208914487678', 'z4lFRwCKGX4'),
                   ('18446744073709551615', '__________8')]
        for value, encoded in vectors:
            for suffix in ('', '_0_140_0_0', '_0_39_26_1_1789212716173352_b063c2da',
                           '_0_39_26_1_1789212716173352_12345678-abcd-1234-5678-123456789abc'):
                with self.subTest(value=value, suffix=suffix):
                    xml = f'<msg><finderFeed><objectId>{value}</objectId><objectNonceId>{value}{suffix}</objectNonceId></finderFeed></msg>'
                    url = mirror.channel_video_body(xml).splitlines()[-1]
                    self.assertEqual(url, f'https://channels.weixin.qq.com/web/pages/feed?oid={encoded}&nid={encoded}')
                    for parameter in parse_qs(urlsplit(url).query).values():
                        binary = base64.urlsafe_b64decode(parameter[0] + '=')
                        self.assertEqual(len(binary), 8)
                        self.assertEqual(int.from_bytes(binary, 'big'), int(value))

    def test_share_url_validation_rejects_lookalikes_credentials_and_resources(self):
        invalid = [
            'https://weixin.qq.com.evil.test/sph/example',
            'https://evil.test/path/weixin.qq.com/sph/example',
            'https://evil.test/?next=https://weixin.qq.com/sph/example',
            'https://weixin.qq.com@suspicious.test/sph/example',
            'https://credentials@weixin.qq.com/sph/example',
            'https://weixin.qq.com:8443/sph/example',
            'https://weixin.qq.com/sph/', 'https://weixin.qq.com/sph/../update',
            'https://weixin.qq.com/sph/example\n?x=1',
            'https://weixin.qq.com/sph/example\\other',
            'https://weixin.qq.com/sph/example?encfilekey=private',
            'https://weixin.qq.com/sph/example?%74oken=private',
            'https://channels.weixin.qq.com/web/pages/feed',
            'https://channels.weixin.qq.com/web/pages/feed?oid=1&nid=not-encoded',
            'https://channels.weixin.qq.com/web/pages/feed?oid=z4lFRwCKGX4&oid=ambiguous&nid=SuZgeu3RR6A',
            'https://channels.weixin.qq.com/update/',
            'https://support.weixin.qq.com/update/',
            'https://wxapp.tc.qq.com/stodownload?encfilekey=private&token=private',
            'javascript:https://weixin.qq.com/sph/example',
        ]
        for url in invalid:
            with self.subTest(url=url):
                raw = '<msg><appmsg><url><![CDATA[' + url + ']]></url></appmsg></msg>'
                self.assertEqual(mirror.channel_video_body(raw), '视频号\n' + UNAVAILABLE)

    def test_supported_original_page_shapes_keep_query_bytes_and_fragment(self):
        urls = [SHARE,
                'https://channels.weixin.qq.com/web/pages/feed?oid=z4lFRwCKGX4&nid=SuZgeu3RR6A&scene=1#anchor',
                'https://channels.weixin.qq.com/web/pages/feed?eid=synthetic-feed&scene=1',
                'https://channels.weixin.qq.com/finder-preview/pages/sph?id=synthetic-share&scene=1',
                'https://weixin.qq.com/sph/example?literal=%26amp%3Bx%3D1&scene=1']
        for url in urls:
            for path in ('url', 'shareUrl'):
                with self.subTest(url=url, path=path):
                    raw = '<msg><appmsg><finderFeed><' + path + '><![CDATA[' + url + ']]></' + path + '></finderFeed></appmsg></msg>'
                    self.assertEqual(mirror.channel_video_body(raw), '视频号\n' + url)

    def test_fragment_and_nested_parameters_cannot_expose_resource_data(self):
        suffixes = [
            '#token=private', '#section?access_token=private',
            '#https://wxapp.tc.qq.com/stodownload?encfilekey=private',
            '?next=https%3A%2F%2Fwxapp.tc.qq.com%2Fstodownload%3Fencfilekey%3Dprivate',
            '?context=signature%3Dprivate', '#%2574oken%253Dprivate',
            '?context=%2574oken%253Dprivate',
            '#%252525252574oken%25252525253Dprivate',
            '?image=https%3A%2F%2Fmmbiz.qpic.cn%2Fsynthetic-cover',
        ]
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                field = '<url><![CDATA[https://weixin.qq.com/sph/example' + suffix + ']]></url>'
                raw = '<msg><appmsg>' + field + '</appmsg></msg>'
                self.assertEqual(mirror.channel_video_body(raw), '视频号\n' + UNAVAILABLE)
                # An unsafe supplied URL does not suppress independently valid IDs.
                raw = '<msg><appmsg>' + field + IDS + '</appmsg></msg>'
                self.assertEqual(mirror.channel_video_body(raw),
                                 '视频号\nhttps://channels.weixin.qq.com/web/pages/feed?oid=z4lFRwCKGX4&nid=SuZgeu3RR6A')

    def test_metadata_is_read_from_the_feed_and_decoded_only_once(self):
        description = '文字 &lt;tag&gt; &amp; 😀'
        raw = ('<msg><appmsg><title>当前微信版本不支持展示该内容，请升级</title>'
               '<finderFeed><desc><![CDATA[' + description + ']]></desc><nickname>作者 &amp; 同伴</nickname>'
               '<mediaList><media><objectId>1</objectId><objectNonceId>256</objectNonceId>'
               '<url>' + html.escape(SHARE) + '</url></media></mediaList></finderFeed>'
               '<refermsg>' + IDS + '</refermsg></appmsg></msg>')
        self.assertEqual(mirror.channel_video_body(raw),
                         '视频号：' + description + '\n作者：作者 & 同伴\n' + UNAVAILABLE)

    def test_missing_invalid_or_unrecognised_ids_fall_back_without_exposing_values(self):
        pairs = [('', '1'), ('1', ''), ('-1', '1'), ('+1', '1'), ('1.5', '1'),
                 ('１', '1'), ('1', '１２３'), ('18446744073709551616', '1'),
                 ('1', '18446744073709551616'), ('1', '-123_0_140_0_0'),
                 ('1', '123_unrecognised'), ('1', '123_0__0_0'),
                 ('1', '123_0_140_0_0_private'), ('1', '123_0_140_0_0_1234_not-a-uuid'),
                 ('1', '123_0_140_0_0?token=private')]
        for oid, nid in pairs:
            with self.subTest(oid=oid, nid=nid):
                xml = ('<msg><appmsg><finderFeed><desc>可读描述 😀</desc><nickname>作者</nickname>'
                       f'<objectId>{oid}</objectId><objectNonceId>{nid}</objectNonceId>'
                       '</finderFeed></appmsg></msg>')
                self.assertEqual(mirror.channel_video_body(xml), '视频号：可读描述 😀\n作者：作者\n' + UNAVAILABLE)

    def test_malformed_xml_and_resource_only_messages_fail_closed(self):
        for xml in ('<msg><url><![CDATA[' + SHARE,
                    '<msg><url>' + SHARE + '</url>',
                    '<msg><finderFeed><mediaList><media><url>https://cdn.example.test/video?token=private</url></media></mediaList></finderFeed></msg>',
                    '<msg><appmsg><url>https://support.weixin.qq.com/update/</url></appmsg></msg>'):
            with self.subTest(xml=xml):
                self.assertEqual(mirror.channel_video_body(xml), '视频号\n' + UNAVAILABLE)

    def test_wda_channel_subtype_retains_the_same_share_information(self):
        raw = '<msg><appmsg><type>51</type><url><![CDATA[' + SHARE + ']]></url>' + IDS + '</appmsg></msg>'
        row = map_row('room', {'type': (51 << 32) | 49, 'localId': 11, 'content': raw}, {})
        self.assertEqual(row['kind_name'], 'channel_video')
        self.assertEqual(mirror.clean_body(row), '视频号\n' + SHARE)


class ChannelDeliveryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MirrorStore(Path(temp.name) / 'state.db')
        self.config = {'binding_id': 1, 'source_account': 'demo', 'wechat_talker': 'room',
                       'feishu_chat_id': 'chat', 'lark_profile': 'fixture', 'lark_app_id': 'app',
                       'initial_cursor': 10, 'sender_map': {'wx': '已确认姓名'}}
        self.store.initialize(self.config)
        self.store.set_control(1, 'running')
        self.sink = Sink()
        patcher = patch.object(mirror, 'run_lark', self.sink)
        patcher.start()
        self.addCleanup(patcher.stop)

    def row(self, identity, text, kind='channel_video'):
        return {'talker': 'room', 'local_id': identity, 'server_id': str(identity),
                'sender_wxid': 'wx', 'sender': '昵称', 'create_time': 1000 + identity,
                'kind_name': kind, 'text': text}

    def texts(self):
        return [args[args.index('--text') + 1] for _, args in self.sink.sent]

    def test_recovery_keeps_attribution_order_and_dedup_with_unavailable_link(self):
        raw = ('<msg>' + IDS + '<avatar>https://cdn.example.test/avatar?token=private</avatar></msg>')
        rows = [self.row(11, raw), self.row(12, '<msg>broken'), self.row(13, '后续文字 😀', 'text')]
        source = History(rows, page_size=1)
        logs = io.StringIO()
        with contextlib.redirect_stdout(logs):
            self.assertEqual(mirror.recover_once(self.config, self.store, source), 3)
            restarted = MirrorStore(self.store.path)
            self.assertEqual(mirror.recover_once(self.config, restarted, source), 0)
        expected = 'https://channels.weixin.qq.com/web/pages/feed?oid=z4lFRwCKGX4&nid=SuZgeu3RR6A'
        self.assertEqual(self.texts(), ['已确认姓名：视频号\n' + expected,
                                      '已确认姓名：视频号\n' + UNAVAILABLE, '已确认姓名：后续文字 😀'])
        self.assertEqual(restarted.get_cursor(), 13)
        self.assertNotIn('private', logs.getvalue())
        self.assertNotIn('https://', logs.getvalue())

    def test_lost_response_reuses_the_saved_payload_and_receipt_after_restart(self):
        source = History([self.row(11, '<msg>' + IDS + '</msg>')])
        self.sink.timeout_after = 1
        with self.assertRaises(mirror.CliError):
            mirror.recover_once(self.config, self.store, source)
        original_texts = self.texts()
        with patch.object(mirror, 'channel_video_body', side_effect=AssertionError('saved plan must not be rerendered')):
            restarted = MirrorStore(self.store.path)
            mirror.recover_once(self.config, restarted, source)
        self.assertEqual(self.texts(), original_texts)
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(restarted.get_cursor(), 11)

    def test_upgrade_does_not_rewrite_previously_rendered_or_sent_messages(self):
        row = self.row(11, '<msg>' + IDS + '</msg>')
        old_body = '视频号\nhttps://channels.weixin.qq.com/web/pages/feed?oid=1&nid=256_0_140_0_0'
        with patch.object(mirror, 'channel_video_body', return_value=old_body):
            with patch.object(mirror, 'run_lark', side_effect=mirror.CliError('synthetic refusal')):
                with self.assertRaises(mirror.CliError):
                    mirror.send_row(self.config, row, {}, self.store)
        restarted = MirrorStore(self.store.path)
        mirror.send_row(self.config, row, {}, restarted)
        mirror.send_row(self.config, row, {}, restarted)
        self.assertEqual(self.texts(), ['已确认姓名：' + old_body])


if __name__ == '__main__':
    unittest.main()

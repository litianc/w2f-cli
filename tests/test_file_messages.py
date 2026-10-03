"""Ordinary WeChat file shares must upload attachments, not just XML titles."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agents import file_messages
from agents.manage import Manager
from agents import wechat_feishu_mirror as mirror
from agents.storage import MirrorStore
from integrations.file_messages import file_share


class FileMessageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'binding_id': 1, 'wechat_talker': 'fixture-room',
                       'feishu_chat_id': 'oc_fixture', 'lark_profile': 'fixture',
                       'sender_map': {'wx_fixture': '测试成员'},
                       'media_dir': str(self.root / 'media')}
        self.store = MirrorStore(self.root / 'state.db')
        self.store.initialize(self.config)
        self.row = {'talker': 'fixture-room', 'local_id': 49, 'server_id': 123,
                    'create_time': 1234, 'sender_wxid': 'wx_fixture', 'kind_name': 'app',
                    'text': '<msg><appmsg><title><![CDATA[测试资料.pdf]]></title><type>74</type>'
                            '<appattach><totallen>12</totallen><fileext><![CDATA[pdf]]></fileext>'
                            '</appattach></appmsg></msg>'}
        self.sent = []

    def source_file(self):
        path = self.root / 'source.pdf'
        path.write_bytes(b'%PDF-fixture')
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        self.row['text'] = self.row['text'].replace('</appmsg>', f'<md5><![CDATA[{digest}]]></md5></appmsg>')
        return path

    def reader(self, path, cmd, **kwargs):
        data = {'resources': []}
        if 'file-resource' in cmd:
            data.update(contract='file_resource_v1', talker='fixture-room', local_id=49,
                        server_id=123, create_time=1234, file=file_share(self.row),
                        resources=[{'local_paths': [str(path)]}] if path.exists() else [])
        return SimpleNamespace(returncode=0, stdout=json.dumps({'data': data}), stderr='')

    def send(self, config, args):
        if '--file' in args:
            path = Path(args[args.index('--file') + 1])
            self.assertEqual(path.name, '测试资料.pdf')
            self.assertEqual(path.read_bytes(), b'%PDF-fixture')
        self.sent.append(args)
        return {'data': {'message_id': f'om_fixture_{len(self.sent)}'}}

    def test_type_74_uploads_attachment_and_does_not_display_cdata(self):
        path = self.root / '测试资料.pdf'
        path.write_bytes(b'%PDF-fixture')
        sent = []
        def send(config, args):
            sent.append(args)
            return {'data': {'message_id': f'om_fixture_{len(sent)}'}}
        with patch.object(mirror, 'stage_media', return_value=(str(path), False)), \
             patch.object(mirror, 'run_lark', side_effect=send):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertTrue(any('--file' in args for args in sent), 'The PDF attachment was never uploaded')
        self.assertNotIn('CDATA', str(sent))

    def test_file_title_unwraps_cdata(self):
        self.assertNotIn('CDATA', mirror.clean_body(self.row))
        self.assertIn('测试资料.pdf', mirror.clean_body(self.row))

    def test_exact_resource_upload_preserves_name_and_replays_without_duplication(self):
        path = self.source_file()
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(path, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.message('fixture-room:49', 1)['kind'], 'file')
        self.assertTrue(path.exists(), 'WeChat source file must remain untouched')

    def test_completed_type_6_primary_resource_keeps_title_at_cli_boundary(self):
        source = self.source_file()
        self.row['kind_name'] = 'file'
        self.row['text'] = self.row['text'].replace('<type>74</type>', '<type>6</type>')
        self.row['content'] = '<?xml version="1.0"?>\n' + self.row['text']
        calls = []
        def cli(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if '--file' in cmd:
                relative = cmd[cmd.index('--file') + 1]
                self.assertEqual(relative, '测试资料.pdf')
                self.assertEqual((Path(kwargs['cwd']) / relative).read_bytes(), b'%PDF-fixture')
            return SimpleNamespace(returncode=0, stdout=json.dumps({'data': {'message_id': f'om_cli_{len(calls)}'}}), stderr='')
        with patch.object(mirror, 'reader_media_result', return_value=([source], False)), \
             patch.object(mirror.subprocess, 'run', side_effect=cli):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(sum('--file' in cmd for cmd, _ in calls), 1)
        self.assertTrue(source.exists())

    def legacy_named_file(self):
        source = self.source_file()
        self.row['kind_name'] = 'file'
        self.row['text'] = self.row['text'].replace('<type>74</type>', '<type>6</type>')
        old = self.root / '1-opaque-49.pdf'
        old.write_bytes(source.read_bytes())
        def send(config, args):
            self.sent.append(args)
            return {'data': {'message_id': f'om_old_{len(self.sent)}'}}
        with patch.object(mirror, 'stage_media', return_value=(str(old), False)), \
             patch.object(mirror, 'run_lark', side_effect=send):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.store.set_cursor(49, 1)
        return source, self.store.message('fixture-room:49', 1)

    def test_legacy_internal_filename_can_be_corrected_once_in_original_reply(self):
        source, message = self.legacy_named_file()
        original = dict(self.store.delivery(message['id'], 'media'))
        manager = Manager(self.store, self.root / 'config.json')
        job = manager.post('media-failures', {'op': 'file-attachment', 'id': message['id']})
        same = manager.post('media-failures', {'op': 'file-attachment', 'id': message['id']})
        self.assertEqual(job['id'], same['id'])
        self.assertEqual(job['state'], 'manual')
        with patch.object(mirror, 'reader_media_result', return_value=([source], False)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.retry_media_jobs(self.config, self.store)
            self.assertEqual(len(self.sent), 2, 'Registration alone must not send')
            manager.post('media-failures', {'op': 'retry', 'id': job['id']})
            mirror.retry_media_jobs(self.config, self.store)
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[-1][:3], ['+messages-reply', '--message-id', original['feishu_message_id']])
        self.assertEqual(dict(self.store.delivery(message['id'], 'media')), original)
        current = self.store.message('fixture-room:49', 1)
        self.assertEqual(current['sender_name'], message['sender_name'])
        self.assertEqual(current['feishu_message_id'], message['feishu_message_id'])
        self.assertEqual(self.store.get_cursor(1), 49)
        self.assertTrue(source.exists())

    def test_correctly_named_or_uncertain_attachment_cannot_register_name_repair(self):
        source, message = self.legacy_named_file()
        receipt = self.store.delivery(message['id'], 'media')
        args = json.loads(receipt['payload_json'])
        for state, name in [('sent', '测试资料.pdf'), ('uncertain', '1-opaque-49.pdf')]:
            with self.subTest(state=state, name=name):
                args[args.index('--file') + 1] = str(self.root / name)
                with self.store.connect() as db:
                    db.execute("UPDATE deliveries SET state=?,payload_json=? WHERE message_id=? AND part='media'", (state, json.dumps(args), message['id']))
                with self.assertRaises(ValueError):
                    file_messages.register_historical(self.store, message['id'])

    def test_missing_file_creates_recoverable_placeholder_then_only_replies_with_attachment(self):
        path = self.source_file()
        payload = path.read_bytes()
        path.unlink()
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(path, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
            self.assertEqual(len(self.sent), 1)
            self.assertIn('文件尚未就绪', str(self.sent))
            job = self.store.list_media_failures()[0]
            path.write_bytes(payload)
            Manager(self.store, self.root / 'config.json').post('media-failures', {'op': 'retry', 'id': job['id']})
            mirror.retry_media_jobs(self.config, self.store)
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][:3], ['+messages-reply', '--message-id', 'om_fixture_1'])
        self.assertEqual(self.store.list_media_failures(), [])

    def test_rejects_partial_wrong_digest_or_other_message_resource(self):
        path = self.source_file()
        for mismatch in ('partial', 'digest', 'identity'):
            with self.subTest(mismatch=mismatch):
                path.write_bytes(b'%PDF-fixture')
                if mismatch == 'partial': path.write_bytes(b'%PDF-')
                if mismatch == 'digest': path.write_bytes(b'%PDF-xxxxxxx')
                def reader(*a, **kw):
                    result = self.reader(path, *a, **kw)
                    if mismatch == 'identity' and 'file-resource' in a[0]:
                        data = json.loads(result.stdout);data['data']['talker'] = 'other-room'
                        result.stdout = json.dumps(data)
                    return result
                with patch.object(mirror.subprocess, 'run', side_effect=reader), self.assertRaises(mirror.CliError):
                    mirror.stage_media({**self.row, 'media_dir': str(self.root / 'media')}, 'file')

    def test_old_file_type_6_and_cdata_entities_are_supported(self):
        row = {**self.row, 'text': self.row['text'].replace('<type>74</type>', '<type>6</type>')}
        self.assertEqual(file_share(row)['name'], '测试资料.pdf')
        row['text'] = row['text'].replace('<![CDATA[测试资料.pdf]]>', 'A &amp; B.pdf')
        self.assertEqual(file_share(row)['name'], 'A & B.pdf')
        row['text'] = row['text'].replace('A &amp; B.pdf', '<![CDATA[A &amp; B.pdf]]>')
        self.assertEqual(file_share(row)['name'], 'A &amp; B.pdf')

    def test_article_and_quoted_file_are_not_classified_as_attachments(self):
        self.assertIsNone(file_share({'text': self.row['text'].replace('<type>74</type>', '<type>5</type>')}))
        self.assertIsNone(file_share({'text': '<msg><appmsg><type>57</type><refermsg>' + self.row['text'] + '</refermsg></appmsg></msg>'}))

    def legacy_message(self):
        # A previously saved render plan and successful title are immutable.
        message = self.store.upsert_message('fixture-room:49', self.row, 'app', binding_id=1)
        message = self.store.message('fixture-room:49', 1)
        self.store.mark_message('fixture-room:49', 'processing', binding_id=1, sender_name='原姓名',
                                idempotency_key=mirror.delivery_key(self.config, 'fixture-room:49', 'message'))
        self.store.save_setting(f'render:{message["id"]}', {'target': ['+messages-send', '--chat-id', 'oc_fixture'],
                                                        'parts': ['原姓名：<![CDATA[测试资料.pdf]]>'], 'placeholder': False})
        with patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
        return self.store.message('fixture-room:49', 1)

    def test_legacy_title_stays_untouched_until_explicit_attachment_repair(self):
        path = self.source_file()
        message = self.legacy_message()
        parent = message['feishu_message_id']
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(path, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
            self.assertEqual(len(self.sent), 1)
            manager = Manager(self.store, self.root / 'config.json')
            job = manager.post('media-failures', {'op': 'file-attachment', 'id': message['id']})
            same = manager.post('media-failures', {'op': 'file-attachment', 'id': message['id']})
            self.assertEqual(job['id'], same['id'])
            mirror.retry_media_jobs(self.config, self.store)
            self.assertEqual(len(self.sent), 1, 'Registration alone must not send')
            manager.post('media-failures', {'op': 'retry', 'id': job['id']})
            mirror.retry_media_jobs(self.config, self.store)
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[-1][:3], ['+messages-reply', '--message-id', parent])
        current = self.store.message('fixture-room:49', 1)
        self.assertEqual(current['feishu_message_id'], parent)
        self.assertEqual(current['sender_name'], '原姓名')

    def test_file_paths_are_isolated_and_unsafe_names_are_sanitized(self):
        path = self.source_file()
        self.row['text'] = self.row['text'].replace('测试资料.pdf', '../../测试资料.pdf')
        with patch.object(mirror, 'reader_media_result', return_value=([path], False)):
            first, _ = mirror.stage_media({**self.row, 'binding_id': 1, 'media_dir': str(self.root / 'media')}, 'file')
            other, _ = mirror.stage_media({**self.row, 'binding_id': 2, 'media_dir': str(self.root / 'media')}, 'file')
        self.assertNotEqual(first, other)
        self.assertTrue(Path(first).resolve().is_relative_to((self.root / 'media').resolve()))
        self.assertNotIn('/', Path(first).name)

    def uncertain_initial(self, source):
        def send(config, args):
            value = self.send(config, args)
            if '--file' in args:
                raise mirror.CliError('response lost', uncertain=True)
            return value
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(source, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=send), self.assertRaises(mirror.CliError):
            mirror.send_row(self.config, self.row, {}, self.store)
        source.unlink()

    def test_unknown_upload_reuses_snapshot_when_source_cache_disappears(self):
        source = self.source_file()
        self.uncertain_initial(source)
        first = self.sent[-1]
        with patch.object(mirror.subprocess, 'run', side_effect=AssertionError('must reuse saved upload')), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(self.sent[-1], first)
        self.assertEqual(self.store.message('fixture-room:49', 1)['state'], 'sent')

    def test_unknown_upload_missing_snapshot_does_not_send_new_placeholder(self):
        source = self.source_file()
        self.uncertain_initial(source)
        Path(self.sent[-1][self.sent[-1].index('--file') + 1]).unlink()
        with patch.object(mirror.subprocess, 'run', side_effect=AssertionError('no lookup')), \
             patch.object(mirror, 'run_lark', side_effect=self.send), self.assertRaises(mirror.DeliveryUncertain):
            mirror.send_row(self.config, self.row, {}, self.store)
        self.assertEqual(len(self.sent), 2)

    def test_staging_directory_symlink_cannot_alias_or_delete_source(self):
        source = self.source_file()
        root = self.root / 'media'
        root.mkdir()
        namespace = '1-' + hashlib.sha256(b'fixture-room').hexdigest()[:16]
        (root / f'{namespace}-49-file').symlink_to(source.parent, target_is_directory=True)
        with patch.object(mirror, 'reader_media_result', return_value=([source], False)), self.assertRaises(mirror.CliError):
            mirror.stage_media({**self.row, 'media_dir': str(root)}, 'file')
        self.assertEqual(source.read_bytes(), b'%PDF-fixture')

    def test_definite_old_failure_can_replace_only_stale_file_path(self):
        source = self.source_file()
        self.row['text'] = self.row['text'].replace('<type>74</type>', '<type>6</type>')
        def reject(config, args):
            if '--file' in args:raise mirror.CliError('rejected')
            return self.send(config, args)
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(source, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=reject):
            mirror.send_row(self.config, self.row, {}, self.store)
        message = self.store.message('fixture-room:49', 1)
        receipt = self.store.delivery(message['id'], 'media')
        old_args = json.loads(receipt['payload_json'])
        old_args[old_args.index('--file') + 1] = str(self.root / 'expired-old-file.pdf')
        with self.store.connect() as db:
            db.execute("UPDATE deliveries SET payload_json=? WHERE message_id=? AND part='media'", (json.dumps(old_args), message['id']))
        job = self.store.list_media_failures()[0]
        Manager(self.store, self.root / 'config.json').post('media-failures', {'op': 'retry', 'id': job['id']})
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(source, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.retry_media_jobs(self.config, self.store)
        self.assertEqual(self.store.delivery(message['id'], 'media')['state'], 'sent')
        actual = self.sent[-1].copy()
        actual[actual.index('--file') + 1] = old_args[old_args.index('--file') + 1]
        self.assertEqual(actual, old_args + ['--idempotency-key', receipt['idempotency_key']])

    def test_pending_file_retries_without_new_chat_and_is_bounded(self):
        from agents.image_original import next_retry_delay
        source = self.source_file()
        source.unlink()
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(source, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
            job = self.store.list_media_failures()[0]
            self.assertEqual(job['state'], 'pending')
            self.assertEqual(next_retry_delay(self.store, 1, 600), 0)
            mirror.retry_media_jobs(self.config, self.store)
            delay = next_retry_delay(self.store, 1, 600)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 30)
            for _ in range(4):
                with self.store.connect() as db:
                    db.execute('UPDATE media_jobs SET updated_at=0 WHERE id=?', (job['id'],))
                mirror.retry_media_jobs(self.config, self.store)
        final = self.store.list_media_failures()[0]
        self.assertEqual(final['state'], 'manual')
        self.assertEqual(final['attempts'], 3)
        self.assertEqual(len(self.sent), 1)

    def test_interrupted_pending_file_retry_remains_scheduled(self):
        source = self.source_file()
        payload = source.read_bytes()
        source.unlink()
        with patch.object(mirror.subprocess, 'run', side_effect=lambda *a, **kw: self.reader(source, *a, **kw)), \
             patch.object(mirror, 'run_lark', side_effect=self.send):
            mirror.send_row(self.config, self.row, {}, self.store)
            job = self.store.list_media_failures()[0]
            # Stop after send_row commits, before retry_media_jobs can adjust state.
            mirror.send_row({**self.config, '_retry_media': True, '_retry_media_job_id': job['id']}, self.row, {}, self.store)
            reopened = MirrorStore(self.store.path)
            self.assertEqual(reopened.list_media_failures()[0]['state'], 'pending')
            source.write_bytes(payload)
            with reopened.connect() as db:
                db.execute('UPDATE media_jobs SET updated_at=0 WHERE id=?', (job['id'],))
            mirror.retry_media_jobs(self.config, reopened)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(reopened.list_media_failures(), [])


if __name__ == '__main__':
    unittest.main()

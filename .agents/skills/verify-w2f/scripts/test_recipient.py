#!/usr/bin/env python3
"""Prevent false acceptance and unsafe I/O in the read-only recipient proof."""
import json
import os
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

import recipient


def fixture(text='W2F-unique-marker 中文 😀\n第二行'):
    event_id, stream_id, binding = (str(uuid.uuid4()) for _ in range(3))
    meta = dict(prepared=True, mode='synthetic', binding_id=binding, stream_id=stream_id,
                talker='synthetic-room', config=dict(expected_release='a' * 40,
                chat_id='oc_test', app_id='cli_test'))
    event = dict(event_id=event_id, stream_id=stream_id, sequence=1,
                 message=dict(local_id=1, create_time=1, local_type=1, kind_name='text',
                              talker=meta['talker'], sender='测试发送者', text=text))
    texts = recipient.attributed_text('测试发送者', text)
    parts = [dict(event_id=event_id, part=i, state='sent', message_id='om_test_' + str(i))
             for i in range(len(texts))]
    delivery = dict(result='api_verified', paused=True, mode='synthetic', cloud_release='a' * 40,
                    events=[event], cloud=dict(binding_id=binding, target_chat_id='oc_test',
                    target_app_id='cli_test', jobs=[dict(event_id=event_id, state='sent')], parts=parts),
                    readbacks=[dict(event_id=event_id, verified=True, parts=[
                        dict(message_id=p['message_id'], verified=True) for p in parts])])
    receipts = [dict(event_id=event_id, part=p['part'], message_id=p['message_id'],
                     chat_id='oc_test', app_id='cli_test', text=texts[p['part']]) for p in parts]
    envelope = dict(ok=True, identity='user', data=dict(items=[
        dict(message_id=p['message_id'], chat_id=p['chat_id'], msg_type='text', deleted=False,
             body=dict(content=json.dumps({'text': p['text']}, ensure_ascii=False)),
             sender=dict(id='cli_test', id_type='app_id', sender_type='app'))
        for p in receipts]))
    return meta, delivery, receipts, envelope


class ReceiptProof(unittest.TestCase):
    def test_multiline_unicode_and_every_long_text_part(self):
        for text in ('中文 😀\n第二行', '字' * 8001):
            with self.subTest(text_length=len(text)):
                meta, delivery, receipts, response = fixture(text)
                with patch.object(recipient, 'renderer_matches') as render:
                    self.assertEqual(recipient.expected_receipts(meta, delivery), receipts)
                    render.assert_called_once_with(meta['config']['expected_release'])
                self.assertEqual(len(recipient.check_messages(response, receipts, 'user')), len(receipts))
        response['data']['items'].pop()
        with self.assertRaisesRegex(recipient.ProofError, 'receipt_not_visible'):
            recipient.check_messages(response, receipts, 'user')

    def test_same_marker_wrong_target_sender_content_or_withdrawn_cannot_pass(self):
        for field, value in [('chat_id', 'oc_other'), ('deleted', True), ('deleted', None),
                             ('msg_type', 'post'), ('body', {'content': '{"text":"W2F-unique-marker"}'})]:
            _, _, receipts, response = fixture()
            response['data']['items'][0][field] = value
            with self.subTest(field=field), self.assertRaises(recipient.ProofError):
                recipient.check_messages(response, receipts, 'user')
        for field, value in [('id', 'cli_other'), ('sender_type', 'user'), ('id_type', 'open_id')]:
            _, _, receipts, response = fixture()
            response['data']['items'][0]['sender'][field] = value
            with self.subTest(sender_field=field), self.assertRaises(recipient.ProofError):
                recipient.check_messages(response, receipts, 'user')

    def test_missing_fields_and_partial_or_duplicate_response_cannot_pass(self):
        for field in ('chat_id', 'sender', 'deleted', 'body'):
            _, _, receipts, response = fixture()
            del response['data']['items'][0][field]
            with self.subTest(field=field), self.assertRaises(recipient.ProofError):
                recipient.check_messages(response, receipts, 'user')
        _, _, receipts, response = fixture()
        response['data']['items'] *= 2
        with self.assertRaisesRegex(recipient.ProofError, 'duplicate_message'):
            recipient.check_messages(response, receipts, 'user')

    def test_bot_envelope_never_proves_user_visibility(self):
        _, _, receipts, response = fixture()
        response['identity'] = 'bot'
        with self.assertRaisesRegex(recipient.ProofError, 'wrong_identity'):
            recipient.check_messages(response, receipts, 'user')

    def test_wrong_run_missing_part_or_wrong_receipt_cannot_be_used(self):
        mutations = [lambda m, d: d.update(paused=False), lambda m, d: d.update(result='failed'),
                     lambda m, d: d['cloud'].update(binding_id=str(uuid.uuid4())),
                     lambda m, d: d['events'][0].update(stream_id=str(uuid.uuid4())),
                     lambda m, d: d['cloud']['parts'].clear(),
                     lambda m, d: d['readbacks'][0]['parts'][0].update(message_id='om_other')]
        for mutate in mutations:
            meta, delivery, _, _ = fixture()
            mutate(meta, delivery)
            with patch.object(recipient, 'renderer_matches'), self.assertRaises(recipient.ProofError):
                recipient.expected_receipts(meta, delivery)

    def test_matching_deployed_renderer_and_unavailable_release(self):
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=recipient.ROOT, text=True).strip()
        recipient.renderer_matches(head)
        with self.assertRaisesRegex(recipient.ProofError, 'renderer_differs_or_unavailable'):
            recipient.renderer_matches('0' * 40)


class CliBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / '.v040-live/run'
        self.state.mkdir(parents=True, mode=0o700)
        self.meta, self.delivery, _, self.envelope = fixture()
        self.evidence = self.state / 'delivery.json'
        recipient.save(self.state / 'run.json', self.meta)
        recipient.save(self.evidence, self.delivery)
        self.patch = patch.object(recipient, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_real_subprocess_boundary_only_issues_explicit_read_commands(self):
        # A disposable executable records argv; it cannot contact Feishu or obtain credentials.
        binary = self.root / 'bin'
        binary.mkdir()
        executable = binary / 'lark-cli'
        log = self.root / 'argv.jsonl'
        auth = dict(identities=dict(user=dict(available=True, verified=True, openId='ou_recipient')),
                    access_token='must-not-be-retained')
        executable.write_text('#!' + os.sys.executable + '\nimport json,sys\n'
            + 'with open(' + repr(str(log)) + ',"a") as out: out.write(json.dumps(sys.argv[1:])+"\\n")\n'
            + 'if "--version" in sys.argv: print("lark-cli test fixture"); sys.exit(0)\n'
            + 'print(json.dumps(' + repr(auth) + ' if "auth" in sys.argv else '
            + repr(self.envelope) + '))\n')
        executable.chmod(0o700)
        with patch.dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH']), \
                patch.object(recipient, 'renderer_matches'):
            proof = recipient.Recipient(self.state, 'selected-profile')
            result = proof.verify(self.evidence)
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(result['result'], 'recipient_verified')
        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[1], ['--profile', 'selected-profile', 'auth', 'status', '--json', '--verify'])
        self.assertEqual(commands[2][2:5], ['api', 'GET', '/open-apis/im/v1/messages/om_test_0'])
        self.assertEqual(commands[2][commands[2].index('--as') + 1], 'user')
        self.assertNotIn('--download-resources', commands[2])
        self.assertNotIn('must-not-be-retained', json.dumps(result))
        self.assertNotIn(self.meta['talker'], json.dumps(result))

    def test_unavailable_identity_stops_before_any_message_read(self):
        auth = dict(identities=dict(bot=dict(available=True, verified=True), user=dict(available=False)))
        result = subprocess.CompletedProcess([], 0, json.dumps(auth), '')
        with patch.object(recipient, 'bounded_run', return_value=result) as runner:
            with self.assertRaisesRegex(recipient.ProofError, 'identity_not_verified'):
                recipient.Recipient(self.state, 'explicit').doctor()
        self.assertEqual(runner.call_count, 2)

    def test_permission_error_is_blocked_and_error_details_are_redacted(self):
        failure = dict(ok=False, error=dict(type='api', code=99991672, message='private-token', hint='private-target'))
        result = subprocess.CompletedProcess([], 1, '', json.dumps(failure))
        with patch.object(recipient, 'bounded_run', return_value=result):
            with self.assertRaises(recipient.ProofError) as raised:
                recipient.Recipient(self.state, 'explicit').cli('im', '+messages-mget')
        self.assertEqual(raised.exception.result, 'blocked')
        self.assertEqual(raised.exception.details['code'], 99991672)
        self.assertNotIn('private-', json.dumps(raised.exception.details))

    def test_bot_proof_is_labelled_separately(self):
        self.envelope['identity'] = 'bot'
        auth = dict(identities=dict(bot=dict(available=True, verified=True, openId='ou_bot')))
        calls = [subprocess.CompletedProcess([], 0, 'fixture-version', '')]
        calls += [subprocess.CompletedProcess([], 0, json.dumps(body), '') for body in (auth, self.envelope)]
        with patch.object(recipient, 'bounded_run', side_effect=calls), patch.object(recipient, 'renderer_matches'):
            result = recipient.Recipient(self.state, 'explicit', 'bot').verify(self.evidence)
        self.assertEqual(result['result'], 'bot_readback_verified')

    def test_foreign_evidence_and_symlinks_cannot_be_read(self):
        foreign = self.root / 'other.json'
        recipient.save(foreign, self.delivery)
        alias = self.state / 'alias.json'
        alias.symlink_to(self.evidence)
        proof = recipient.Recipient(self.state, 'explicit')
        for path in (foreign, alias):
            with self.subTest(path=path), self.assertRaises(recipient.ProofError):
                proof.verify(path)

    def test_public_json_permissions_produce_private_blocked_evidence(self):
        self.evidence.chmod(0o644)
        args = ['recipient.py', '--state', str(self.state), '--profile', 'explicit',
                'verify', '--evidence', str(self.evidence)]
        with patch.object(os.sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(recipient.main(), 1)
        summary = json.loads(output.getvalue())
        report = recipient.private_json(summary['evidence'])
        self.assertEqual(report['result'], 'blocked')
        self.assertEqual(report['error_code'], 'credentials_permissions')
        self.assertEqual(report['commands'], [])

    def test_bounded_runner_terminates_timed_out_process(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            recipient.bounded_run([os.sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05)


if __name__ == '__main__':
    unittest.main()

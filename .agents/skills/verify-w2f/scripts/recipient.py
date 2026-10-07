#!/usr/bin/env python3
"""Read-only Lark CLI proof that a recipient can fetch W2F's delivered text."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from live import ROOT, bounded_run, private_json, save
from client.collector import CollectionError
from w2f_protocol.ingest import validate_event
from w2f_protocol.text import attributed_text


class ProofError(RuntimeError):
    def __init__(self, reason, result='blocked', details=None):
        super().__init__(reason)
        self.reason, self.result, self.details = reason, result, details or {}


def require(value, reason):
    if not value:
        raise ProofError(reason, 'failed')


def renderer_matches(release):
    """Use the deployed rendering contract, never silently guess an older layout."""
    for name in ('cloud/delivery.py', 'w2f_protocol/text.py'):
        result = bounded_run(['git', 'show', release + ':' + name], cwd=ROOT, timeout=10)
        if result.returncode or result.stdout != (ROOT / name).read_text(encoding='utf-8'):
            raise ProofError('deployed_renderer_differs_or_unavailable')


def expected_receipts(meta, delivery):
    require(meta.get('prepared') is True, 'run_not_prepared')
    config = meta['config']
    release = config['expected_release']
    require(isinstance(release, str) and re.fullmatch(r'[0-9a-f]{40}', release), 'invalid_release')
    require(delivery.get('result') == 'api_verified' and delivery.get('paused') is True,
            'successful_paused_delivery_required')
    require(delivery.get('mode') == meta['mode'] and delivery.get('cloud_release') == release,
            'delivery_run_mismatch')
    cloud = delivery['cloud']
    require(cloud['binding_id'] == meta['binding_id']
            and cloud['target_chat_id'] == config['chat_id']
            and cloud['target_app_id'] == config['app_id'], 'delivery_target_mismatch')
    require(re.fullmatch(r'oc_[A-Za-z0-9_-]{1,128}', config['chat_id'])
            and re.fullmatch(r'cli_[A-Za-z0-9_]{1,128}', config['app_id']), 'invalid_target')
    events = delivery['events']
    require(isinstance(events, list) and 1 <= len(events) <= 3, 'bounded_samples_required')
    receipts, event_ids = [], set()
    for event in events:
        validate_event(event)
        event_id = event['event_id']
        require(event_id not in event_ids and event['stream_id'] == meta['stream_id']
                and event['message']['talker'] == meta['talker'], 'source_run_mismatch')
        event_ids.add(event_id)
        jobs = [j for j in cloud['jobs'] if j['event_id'] == event_id]
        require(len(jobs) == 1 and jobs[0]['state'] == 'sent', 'event_not_sent')
        row = event['message']
        texts = attributed_text(str(row.get('sender') or '未知发言人')[:256], row['text'])
        parts = sorted((p for p in cloud['parts'] if p['event_id'] == event_id), key=lambda p: p['part'])
        require(len(parts) == len(texts), 'incomplete_parts')
        readbacks = [r for r in delivery['readbacks'] if r['event_id'] == event_id]
        require(len(readbacks) == 1 and readbacks[0].get('verified') is True, 'missing_sender_readback')
        verified = readbacks[0]['parts']
        require(len(verified) == len(parts) and all(p.get('verified') is True for p in verified),
                'incomplete_sender_readback')
        require({p['message_id'] for p in verified} == {p['message_id'] for p in parts},
                'sender_receipt_mismatch')
        for index, (part, text) in enumerate(zip(parts, texts)):
            require(type(part['part']) is int and part['part'] == index and part['state'] == 'sent',
                    'part_not_sent_or_out_of_order')
            message_id = part['message_id']
            require(isinstance(message_id, str) and re.fullmatch(r'om_[A-Za-z0-9_-]{1,128}', message_id),
                    'invalid_message_id')
            receipts.append(dict(event_id=event_id, part=index, message_id=message_id,
                                 chat_id=config['chat_id'], app_id=config['app_id'], text=text))
    require(len({p['message_id'] for p in receipts}) == len(receipts), 'duplicate_receipts')
    require(1 <= len(receipts) <= 50, 'receipt_limit_exceeded')
    renderer_matches(release)
    return receipts


def check_messages(envelope, receipts, identity):
    require(envelope.get('ok') is True and envelope.get('identity') == identity,
            'wrong_identity_or_unsuccessful_response')
    rows = envelope['data']['items']
    require(isinstance(rows, list), 'invalid_message_array')
    wanted = {p['message_id']: p for p in receipts}
    observed, seen = [], set()
    for row in rows:
        message_id = row.get('message_id')
        require(message_id in wanted and message_id not in seen, 'unexpected_or_duplicate_message')
        seen.add(message_id)
        expected = wanted[message_id]
        sender = row.get('sender') or {}
        try:
            content = json.loads(row['body']['content'])
        except (KeyError, TypeError, ValueError):
            content = None
        checks = dict(chat=row.get('chat_id') == expected['chat_id'],
                      type=row.get('msg_type') == 'text', deleted=row.get('deleted') is False,
                      sender=sender.get('id_type') == 'app_id' and sender.get('sender_type') == 'app'
                      and sender.get('id') == expected['app_id'],
                      text=content == {'text': expected['text']})
        # Keep only matched receipt fields; never persist unrelated chat messages or CLI stderr.
        observed.append(dict(event_id=expected['event_id'], part=expected['part'],
                             message_id=message_id, chat_id=row.get('chat_id'),
                             sender_id=sender.get('id'), create_time=row.get('create_time'),
                             checks=checks, text_sha256=hashlib.sha256(
                                 str(content.get('text', '') if isinstance(content, dict) else '').encode()).hexdigest()))
    require(seen == set(wanted), 'receipt_not_visible_or_missing')
    if not all(all(item['checks'].values()) for item in observed):
        raise ProofError('recipient_message_mismatch', 'failed', dict(messages=observed))
    return observed


class Recipient:
    def __init__(self, state, profile, identity='user', timeout=30):
        self.state = Path(state).resolve()
        if (not self.state.is_relative_to(ROOT / '.v040-live') or not self.state.is_dir()
                or self.state.stat().st_mode & 0o077):
            raise ProofError('existing_private_live_state_required')
        if not profile.strip() or profile.startswith('-') or not 1 <= timeout <= 120:
            raise ProofError('invalid_cli_options')
        self.profile, self.identity, self.timeout = profile, identity, timeout
        self.commands = []
        self.receiver = None
        self.version = None
        self.messages = []
        self.context = {}

    def cli(self, *args):
        argv = ['lark-cli', '--profile', self.profile, *args]
        self.commands.append(argv)
        result = bounded_run(argv, cwd=ROOT, timeout=self.timeout)
        try:
            body = json.loads(result.stdout if not result.returncode else result.stderr)
        except ValueError:
            raise ProofError('cli_output_unavailable', details=dict(exit_code=result.returncode)) from None
        if result.returncode or body.get('ok') is False:
            error = body.get('error') or {}
            # Messages/hints may include private data; retain structured error codes only.
            raise ProofError('cli_read_unavailable', details=dict(exit_code=result.returncode,
                error_type=error.get('type'), error_subtype=error.get('subtype'), code=error.get('code'),
                missing_scopes=[s for s in error.get('missing_scopes', [])
                    if isinstance(s, str) and re.fullmatch(r'[a-z0-9:._]+', s)]))
        return body

    def doctor(self):
        version = bounded_run(['lark-cli', '--version'], cwd=ROOT, timeout=self.timeout)
        if version.returncode:
            raise ProofError('cli_version_unavailable')
        self.version = version.stdout.strip()[:160]
        status = self.cli('auth', 'status', '--json', '--verify')
        selected = status.get('identities', {}).get(self.identity, {})
        if selected.get('available') is not True or selected.get('verified') is not True:
            raise ProofError('requested_identity_not_verified')
        self.receiver = dict(identity=self.identity, open_id=selected.get('openId'),
                             user_name=selected.get('userName'))
        require(bool(self.receiver['open_id']), 'missing_recipient_identity')
        return self.receiver

    def verify(self, evidence):
        # Resolve neither caller-supplied symlinks nor a delivery from another run.
        path = Path(evidence)
        if path.is_symlink() or path.resolve().parent != self.state:
            raise ProofError('delivery_evidence_must_belong_to_state')
        meta, prior = private_json(self.state / 'run.json'), private_json(path)
        self.context = dict(source_evidence=str(path),
                            delivery_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                            cloud_release=prior.get('cloud_release'))
        receipts = expected_receipts(meta, prior)
        receiver = self.doctor()
        for receipt in receipts:
            # No typed single-message get exists. The batch shortcut also demands
            # unrelated p2p/reaction scopes and converts text; use the exact raw GET.
            response = self.cli('api', 'GET', '/open-apis/im/v1/messages/' + receipt['message_id'],
                                '--as', self.identity, '--format', 'json')
            self.messages.extend(check_messages(response, [receipt], self.identity))
        return dict(result='recipient_verified' if self.identity == 'user' else 'bot_readback_verified',
                    receiver=receiver, messages=self.messages,
                    delivery_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    source_evidence=str(path), cloud_release=prior['cloud_release'],
                    paused=prior['paused'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--as', dest='identity', choices=['user', 'bot'], default='user')
    parser.add_argument('--timeout', type=int, default=30, help='per CLI operation, seconds (1–120)')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    sub.add_parser('verify').add_argument('--evidence', required=True)
    args = parser.parse_args()
    proof, destination = None, None
    started = time.time()
    try:
        proof = Recipient(args.state, args.profile, args.identity, args.timeout)
        destination = proof.state / ('recipient-' + args.command + '-' + str(time.time_ns()) + '.json')
        if args.command == 'doctor':
            result = dict(result='passed', receiver=proof.doctor(), message_read_permission='not_tested')
        else:
            result = proof.verify(args.evidence)
        code = 0
    except ProofError as exc:
        result = dict(result=exc.result, reason=exc.reason, **exc.details)
        code = 1
    except (OSError, ValueError, KeyError, TypeError, CollectionError, subprocess.TimeoutExpired) as exc:
        result = dict(result='blocked', reason='input_or_cli_unavailable', error_type=type(exc).__name__,
                      error_code=getattr(exc, 'code', None))
        code = 1
    result.update(started_at=started, finished_at=time.time(), profile=args.profile,
                  requested_identity=args.identity, commands=proof.commands if proof else [],
                  cli_version=proof.version if proof else None,
                  receiver=proof.receiver if proof else None,
                  helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if code and proof:
        result['messages'] = proof.messages + result.get('messages', [])
    if proof:
        result.update(proof.context)
    if destination:
        save(destination, result)
    print(json.dumps(dict(result=result['result'], reason=result.get('reason'),
                         messages=len(result.get('messages', [])), evidence=str(destination) if destination else None)))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

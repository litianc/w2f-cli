"""Text intake v1: explicit event receipts, canonical identity and bounded requests."""
import hashlib
import json
import uuid

VERSION = 'w2f-text-ingest-v1'
MAX_BODY = 2 * 1024 * 1024
MAX_EVENT = 256 * 1024
MAX_BATCH = 100


class ProtocolError(ValueError):
    pass


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ProtocolError('Expected canonical UUID') from None
    return value


def integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9223372036854775807


def text(value, maximum=256):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode('utf-8')) <= maximum and '\x00' not in value


def encode(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProtocolError('Invalid JSON value') from None


def digest(event):
    return hashlib.sha256(encode(event)).hexdigest()


def validate_event(event):
    if not isinstance(event, dict) or set(event) != {'sequence', 'event_id', 'stream_id', 'message'}:
        raise ProtocolError('Invalid event envelope')
    identifier(event['event_id'])
    identifier(event['stream_id'])
    message = event['message']
    if (not integer(event['sequence'], 1) or not isinstance(message, dict)
            or not integer(message.get('local_id'), 1) or not integer(message.get('create_time'))
            or not text(message.get('talker')) or message.get('kind_name') != 'text'
            or type(message.get('local_type')) is not int or message['local_type'] != 1
            or not isinstance(message.get('text'), str)):
        raise ProtocolError('Invalid ordinary text event')
    if len(encode(event)) > MAX_EVENT:
        raise ProtocolError('Event too large')
    # PostgreSQL JSONB cannot store NUL in strings, including optional reader metadata.
    def valid_strings(item):
        if isinstance(item, str):
            return '\x00' not in item
        if isinstance(item, dict):
            return all(valid_strings(k) and valid_strings(v) for k, v in item.items())
        if isinstance(item, list):
            return all(valid_strings(v) for v in item)
        return True
    if not valid_strings(message):
        raise ProtocolError('Invalid text encoding')
    return event


def validate_batch(body):
    if (not isinstance(body, dict) or set(body) != {'protocol', 'device_id', 'source_account', 'auth_version', 'events'}
            or body['protocol'] != VERSION or not text(body['source_account'])
            or not integer(body['auth_version'], 1) or not isinstance(body['events'], list)
            or not 1 <= len(body['events']) <= MAX_BATCH):
        raise ProtocolError('Invalid intake request')
    identifier(body['device_id'])
    seen, previous = set(), 0
    stream = None
    for event in body['events']:
        validate_event(event)
        if event['event_id'] in seen or event['sequence'] <= previous:
            raise ProtocolError('Events must be unique and ordered')
        if stream is not None and stream != event['stream_id']:
            raise ProtocolError('One source stream per request')
        seen.add(event['event_id'])
        previous, stream = event['sequence'], event['stream_id']
    return body

"""Read-only operator proof of a delivered event; run in the sender container."""
import argparse
import json
import psycopg

from cloud.sender import settings, transport
from cloud.store import Store, StoreError
from cloud.feishu import DeliveryError
from w2f_protocol.ingest import identifier, ProtocolError


def verify(store, adapter, binding, event):
    identifier(binding)
    identifier(event)
    with store.connection() as db:
        row=db.execute('''SELECT b.target_app_id,m.user_id,j.state FROM w2f_messages m
            JOIN w2f_bindings b ON b.id=m.binding_id JOIN w2f_delivery_jobs j USING(event_id)
            WHERE b.id=%s AND m.event_id=%s''',(binding,event)).fetchone()
        parts=db.execute('SELECT * FROM w2f_delivery_parts WHERE event_id=%s ORDER BY part',(event,)).fetchall()
    if not row or row['state']!='sent' or not parts or any(p['state']!='sent' for p in parts):
        raise StoreError('event_not_delivered',409)
    observed=[adapter.verify_text(p['message_id'],row['target_app_id'],row['user_id'],p['payload']) for p in parts]
    return dict(verified=True,event_id=event,parts=observed)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binding',required=True)
    parser.add_argument('--event',required=True)
    args=parser.parse_args()
    try:
        result=verify(Store(settings()),transport(),args.binding,args.event)
    except (StoreError,DeliveryError,ProtocolError,psycopg.Error,OSError,ValueError) as exc:
        print(json.dumps(dict(verified=False,error=getattr(exc,'code','verification_unavailable'))))
        return 1
    print(json.dumps(result))
    return 0


if __name__=='__main__':
    raise SystemExit(main())

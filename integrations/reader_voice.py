"""Read a single voice BLOB using existing reader credentials and snapshots."""
import base64
import re
from pathlib import Path
try:
    from .reader_sync import connection
except ImportError:
    from reader_sync import connection


def read_voice(reader, db, args):
    talker = reader.resolve_username(db, args.talker)
    candidates = {}
    # Only siblings of the explicitly configured message DBs for this account.
    sources = sorted({p for configured in db.message_dbs for p in Path(configured).parent.glob('media_*.db')
                      if re.fullmatch(r'media_[0-9]+\.db', p.name)})
    errors = 0
    for source in sources:
        try:
            with connection(db, source) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'VoiceInfo', 'Name2Id'} <= tables:
                    continue
                # Received voices use server ID; local outgoing voices can still
                # have svr_id=0. Only the exact group/local ID/time triple may
                # compensate for an unfilled server ID, never a fuzzy match.
                rows = conn.execute("""SELECT DISTINCT substr(v.voice_data,1,16777217) AS voice_data FROM VoiceInfo v
                    JOIN Name2Id n ON n.rowid=v.chat_name_id
                    WHERE n.user_name=? AND ((v.svr_id=? AND v.svr_id!=0)
                      OR (v.svr_id=0 AND v.local_id=? AND v.create_time=?))
                    LIMIT 2""", (talker,args.server_id,args.local_id,args.create_time)).fetchall()
                for row in rows:
                    value = bytes(row[0] or b'')
                    if not 10 <= len(value) <= 16777216:
                        raise ValueError('invalid voice resource size')
                    if value.startswith((b'\x02#!SILK_V3', b'#!SILK_V3', b'#!AMR')):
                        candidates[value] = '.amr' if value.startswith(b'#!AMR') else '.silk'
        except Exception:
            errors += 1  # Never expose database keys, paths or decrypted bytes.
    if errors or len(candidates) != 1:
        code = 'voice_ambiguous' if len(candidates)>1 else ('voice_database_unreadable' if errors else 'voice_resource_missing')
        raise reader.ReaderError('未取得唯一匹配的本机语音资源',code)
    data, extension = next(iter(candidates.items()))
    return {'contract':'voice_resource_v1','talker':talker,'local_id':args.local_id,
            'extension':extension,'base64':base64.b64encode(data).decode('ascii')}

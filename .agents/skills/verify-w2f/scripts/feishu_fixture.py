"""Private HTTP Feishu fixture: validate the wire request, persist attempts and receipts."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import time
import uuid

ROOT=Path('/fixture')


def connect():
    db=sqlite3.connect(ROOT/'sink.sqlite3',timeout=15)
    db.row_factory=sqlite3.Row
    return db


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def reply(self,status,body):
        raw=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Retry-After','1')
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        prefix='/open-apis/im/v1/messages/'
        if self.path.startswith(prefix):
            app=self.headers.get('Authorization','').removeprefix('Bearer fixture-token-')
            with connect() as db:
                row=db.execute('SELECT * FROM receipts WHERE message_id=? AND app=?',(self.path[len(prefix):],app)).fetchone()
            if not row:
                self.reply(404,dict(code=230001)); return
            request=json.loads(row['request'])
            current=json.loads((ROOT/'control.json').read_text()).get('modes',{}).get(row['chat'])
            chat=row['chat']+'_wrong' if current=='wrong_readback_target' else row['chat']
            self.reply(200,dict(code=0,data=dict(items=[dict(message_id=row['message_id'],chat_id=chat,
                sender=dict(id=row['app'],id_type='app_id',sender_type='app'),msg_type='text',deleted=False,
                body=dict(content=request['content']))])))
            return
        self.reply(200,dict(status='fixture'))
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path=='/open-apis/auth/v3/tenant_access_token/internal':
            allowed=json.loads((ROOT/'control.json').read_text())['apps']
            if allowed.get(body.get('app_id')) != body.get('app_secret'):
                self.reply(400,dict(code=10003)); return
            self.reply(200,dict(code=0,tenant_access_token='fixture-token-'+body['app_id'],expire=7200)); return
        if self.path!='/open-apis/im/v1/messages?receive_id_type=chat_id':
            self.reply(404,dict(code=404)); return
        app=self.headers.get('Authorization','').removeprefix('Bearer fixture-token-')
        if not app.startswith('cli_verify_') or set(body)!={'receive_id','msg_type','content','uuid'} or body['msg_type']!='text':
            self.reply(400,dict(code=230001)); return
        content=json.loads(body['content'])
        assert set(content)=={'text'} and isinstance(content['text'],str)
        assert str(uuid.UUID(body['uuid']))==body['uuid']
        chat=body['receive_id']
        control=json.loads((ROOT/'control.json').read_text())
        mode=control.get('modes',{}).get(chat,'success')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count=db.execute('SELECT COUNT(*) FROM attempts WHERE chat=?',(chat,)).fetchone()[0]
            db.execute('INSERT INTO attempts(app,chat,request,mode) VALUES(?,?,?,?)',(app,chat,json.dumps(body,ensure_ascii=False),mode))
            if mode=='rate' or (mode=='rate_once' and count==0):
                response=(429,dict(code=230020))
            elif mode=='reject':
                response=(400,dict(code=230002))
            elif mode=='server_error':
                response=(503,dict(code=500))
            else:
                old=db.execute('SELECT * FROM receipts WHERE app=? AND request_id=?',(app,body['uuid'])).fetchone()
                if old:
                    assert json.loads(old['request'])==body
                    message_id=old['message_id']
                else:
                    message_id='om_fixture_'+uuid.uuid4().hex
                    db.execute('INSERT INTO receipts(app,request_id,chat,request,message_id) VALUES(?,?,?,?,?)',
                               (app,body['uuid'],chat,json.dumps(body,ensure_ascii=False),message_id))
                response=(200,dict(code=0,data=dict(message_id=message_id)))
        if mode in {'drop','hold'}:
            if mode=='hold':
                for _ in range(150):
                    if (ROOT/'release-hold').exists():
                        break
                    time.sleep(.1)
            if mode=='drop':
                self.close_connection=True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
        if mode=='being_sent':
            response=(400,dict(code=230049))
        if mode=='missing_id':
            response=(200,dict(code=0,data={}))
        self.reply(*response)


if __name__=='__main__':
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,app TEXT,chat TEXT,request TEXT,mode TEXT);
            CREATE TABLE IF NOT EXISTS receipts(app TEXT,request_id TEXT,chat TEXT,request TEXT,message_id TEXT,
            UNIQUE(app,request_id));''')
    ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()

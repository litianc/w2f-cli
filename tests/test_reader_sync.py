import argparse
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from integrations.reader_sync import read_page
from integrations.forward_records import parse_record


class ReaderPagingAcceptance(unittest.TestCase):
    def test_T51_sql_pagination_exceeds_old_5000_scan_window(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path=Path(directory)/'source.db';table='Msg_fixture'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE Name2Id(user_name TEXT)');db.execute("INSERT INTO Name2Id VALUES('wx')")
                db.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,message_content TEXT)')
                db.executemany(f'INSERT INTO {table} VALUES(?,?,1,?,1,?)',[(i,90000+i,1000+i//3,f'第{i}句 😀') for i in range(1,5504)])
            db.close()
            def connect(path):
                c=sqlite3.connect(f'file:{path}?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c
            database=SimpleNamespace(message_dbs=[db_path],connect=connect,contact_index=lambda:{},self_username='self')
            reader=SimpleNamespace(resolve_username=lambda db,chat:chat,message_table=lambda chat:table,message_projection=lambda db,t:'*',visible_message_content=lambda text,*args:text,decode_message_content=lambda row:row['message_content'],message_kind=lambda *args:'text',time_iso=str,ReaderError=lambda msg,code:RuntimeError(code))
            args=argparse.Namespace(talker='room',after_local_id=3,through_local_id=None,page_after_time=None,page_after_local_id=0,limit=97)
            ids=[]
            while True:
                page=read_page(reader,database,args);ids.extend(r['local_id'] for r in page['messages'])
                if not page['has_more']:break
                args.through_local_id=page['through_local_id'];args.page_after_time=page['next']['time'];args.page_after_local_id=page['next']['local_id']
            self.assertEqual(ids,list(range(4,5504)))
            self.assertEqual(len(ids),len(set(ids)))
            args.after_local_id=99999
            with self.assertRaisesRegex(RuntimeError,'source_gap'):read_page(reader,database,args)

    def test_forward_entities_ids_nested_and_real_datatypes(self):
        raw='<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><sourcename>甲</sourcename><dataitemsource><realchatname>wx_1</realchatname></dataitemsource><datadesc>A &amp; B 😀</datadesc></dataitem><dataitem datatype="2"><sourcename>乙</sourcename></dataitem><dataitem datatype="17"><sourcename>丙</sourcename><recorditem>&lt;datalist&gt;&lt;dataitem datatype="1"&gt;&lt;sourcename&gt;丁&lt;/sourcename&gt;&lt;datadesc&gt;嵌套句&lt;/datadesc&gt;&lt;/dataitem&gt;&lt;/datalist&gt;</recorditem></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>'
        items=parse_record(raw)
        self.assertEqual(items[0]['body'],'A & B 😀');self.assertEqual(items[0]['sender_id'],'wx_1')
        self.assertEqual(items[1]['kind'],'image');self.assertEqual(items[2]['children'][0]['body'],'嵌套句')

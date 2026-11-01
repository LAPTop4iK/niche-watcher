#!/usr/bin/env python3
"""Send exactly the explicitly requested summary event; leave other pending mail alone."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import notifications
import scanner as sc
import tg_digest
import tg_setup


def deliver(db,event_key,out,send=False):
    checked=datetime.now(timezone.utc)
    with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as con:
        con.execute('BEGIN');message=tg_digest.build_message(con,checked)
    report=dict(created_at=checked.isoformat(),event_key=event_key,mode='current-search-summary',
                payload_sha256=hashlib.sha256(message.encode()).hexdigest(),payload=message,status='PREVIEW',messages_sent=0)
    def sender(text):
        if len(text)>3800 or len(text.encode('utf-16-le'))//2>3800:raise ValueError('Report must fit one message')
        token=os.environ.get('TG_BOT_TOKEN');chat=os.environ.get('TG_CHAT_ID')
        if not token or not chat:raise ValueError('Telegram credentials missing')
        body=tg_setup.call(token,'sendMessage',dict(chat_id=chat,text=text,parse_mode='HTML',disable_web_page_preview=True))
        result=body.get('result') or {}
        report.update(telegram_message_id=result.get('message_id'),acknowledged_at=sc.iso(sc.now_utc()),messages_sent=1)
        if isinstance(result.get('date'),int):report['telegram_sent_at']=datetime.fromtimestamp(result['date'],timezone.utc).isoformat()
        return True
    try:
        if send:
            with sqlite3.connect(db) as con:
                notifications.enqueue(con,event_key,message)
                stored=con.execute('SELECT payload,delivered_at FROM notification_outbox WHERE event_key=?',(event_key,)).fetchone()
                report.update(payload=stored[0],payload_sha256=hashlib.sha256(stored[0].encode()).hexdigest(),previous_delivery_at=stored[1])
                delivered,failed=notifications.flush(con,sender,limit=1,event_key=event_key)
                status=con.execute('SELECT status FROM notification_outbox WHERE event_key=?',(event_key,)).fetchone()[0]
                report.update(status=status,delivered_this_call=delivered,failures=failed)
                if failed:report['error']=con.execute('SELECT last_error FROM notification_outbox WHERE event_key=?',(event_key,)).fetchone()[0]
    finally:
        Path(out).parent.mkdir(parents=True,exist_ok=True)
        Path(out).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default='niches.db');ap.add_argument('--event-key',required=True);ap.add_argument('--out',required=True);ap.add_argument('--send',action='store_true');args=ap.parse_args()
    import db_store
    db_store.ensure(args.db)
    result=deliver(args.db,args.event_key,args.out,args.send)
    # Never log token/chat identifiers or exception URLs.
    print(json.dumps({k:result.get(k) for k in ('status','messages_sent','telegram_message_id','acknowledged_at','error')},ensure_ascii=False))
    if args.send and result['status']!='SENT':raise SystemExit('Report not acknowledged; see receipt')


if __name__=='__main__':main()

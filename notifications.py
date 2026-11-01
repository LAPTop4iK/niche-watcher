"""Durable Telegram outbox. A failed delivery remains pending for the next run."""
from datetime import datetime,timezone,timedelta
import sqlite3

SCHEMA='''CREATE TABLE IF NOT EXISTS notification_outbox (
 event_key TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING',attempts INTEGER NOT NULL DEFAULT 0,
 last_attempt_at TEXT,delivered_at TEXT,last_error TEXT
)'''


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def prepare(con):
    con.execute(SCHEMA)
    con.commit()


def enqueue(con,event_key,payload):
    prepare(con)
    con.execute("INSERT INTO notification_outbox (event_key,created_at,payload) VALUES (?,?,?) ON CONFLICT(event_key) DO UPDATE SET payload=excluded.payload WHERE notification_outbox.status='PENDING'",
                (event_key,now(),payload))
    con.commit()


def flush(con,send,limit=10):
    prepare(con)
    rows=con.execute("SELECT event_key,payload FROM notification_outbox WHERE status='PENDING' ORDER BY created_at,event_key LIMIT ?",(limit,)).fetchall()
    delivered=failed=0
    for key,payload in rows:
        try:
            ok=bool(send(payload));error=None if ok else 'sendMessage not acknowledged'
        except Exception as exc:
            ok=False;error=type(exc).__name__
        con.execute('''UPDATE notification_outbox SET attempts=attempts+1,last_attempt_at=?,
            status=?,delivered_at=?,last_error=? WHERE event_key=?''',
            (now(),'SENT' if ok else 'PENDING',now() if ok else None,error,key))
        con.commit()
        delivered+=ok;failed+=not ok
        if not ok:
            break  # a broken connection should not multiply outgoing attempts
    return delivered,failed

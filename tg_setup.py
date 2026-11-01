#!/usr/bin/env python3
"""Read bot identity and discover a newly messaged chat. Sending needs --send-test.

Use TG_BOT_TOKEN or enter the token interactively. getUpdates is not consumed
merely by reading it: acknowledgement uses an offset, which this helper omits.
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.request


def call(token,method,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',data=data,
                               headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=15) as response:
        body=json.load(response)
    if body.get('ok') is not True:
        raise RuntimeError('Telegram did not acknowledge the request')
    return body


def new_chats(updates,not_before):
    found={}
    for update in updates:
        message=update.get('message') or update.get('edited_message') or {}
        if message.get('date',0)<not_before:
            continue
        chat=message.get('chat') or {}
        if chat.get('id') is not None:
            found[chat['id']]=chat.get('title') or chat.get('username') or chat.get('first_name','')
    return found


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--token',help='prefer TG_BOT_TOKEN or interactive entry')
    ap.add_argument('--chat-id',type=int)
    ap.add_argument('--send-test',action='store_true',help='send one test to the specified/new chat')
    args=ap.parse_args()
    token=args.token or os.environ.get('TG_BOT_TOKEN') or getpass.getpass('Telegram bot token: ')
    try:
        me=call(token,'getMe')['result']
    except Exception as error:
        raise SystemExit('Bot identity check failed: '+type(error).__name__)
    print('Bot verified: @'+me.get('username',''))
    cid=args.chat_id
    if cid is None:
        print('Send /start to this bot now; waiting up to 60 seconds.')
        not_before=int(time.time());deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            chats=new_chats(call(token,'getUpdates',{'timeout':0}).get('result',[]),not_before)
            if len(chats)>1:
                print('Multiple recent chats found:',chats)
                raise SystemExit('Run again with an explicit --chat-id')
            if chats:
                cid=next(iter(chats));break
            time.sleep(2)
    if cid is None:
        raise SystemExit('No new message received; no configuration changed')
    print('TG_CHAT_ID =',cid)
    if args.send_test:
        call(token,'sendMessage',{'chat_id':cid,'text':'Niche Watcher: Telegram delivery test.'})
        print('Test acknowledged by Telegram')
    else:
        print('No messages sent. Configure TG_BOT_TOKEN and TG_CHAT_ID in Actions secrets.')


if __name__=='__main__':
    main()

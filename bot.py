#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pprint import pprint
from typing import Any
from typing import Dict
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask
from flask import jsonify
from flask import request

app = Flask(__name__)
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:10]

print(f"Webhook URL: /webhook/{WEBHOOK_SECRET}/")


class Persistence:
    def __init__(self, filename: str = "state.sqlite3"):
        self._con = sqlite3.connect(filename)
        self._cur = self._con.cursor()

        self._cur.execute(
            'CREATE TABLE IF NOT EXISTS "user_to_chat" ( "username" TEXT, "chat_id" INTEGER UNIQUE );'
        )
        self._cur.execute(
            'CREATE TABLE IF NOT EXISTS "current_item" ( "id" INTEGER UNIQUE, "item" INTEGER );'
        )
        self._cur.execute(
            'CREATE INDEX IF NOT EXISTS "username" ON "user_to_chat" ( "username" );'
        )
        self._cur.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS "chat_id" ON "user_to_chat" ( "chat_id" );'
        )

        self._dict: Dict[str, int] = {}

    def get_chat_by_username(self, username: str) -> Optional[int]:
        """Get a user's chat ID by the monitored username."""
        self._cur.execute(
            "SELECT chat_id FROM user_to_chat WHERE username = ?", [username]
        )
        result = self._cur.fetchone()
        return result[0] if result else None

    def set_chat_by_username(self, username: str, chat_id: int) -> None:
        """Set a user's chat ID for the monitored username."""
        self._cur.execute(
            "INSERT INTO user_to_chat(username, chat_id) VALUES(?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET username=?;",
            [username, chat_id, username],
        )
        self._cur.fetchall()
        self._con.commit()

    def set_current_item(self, current_item: int) -> None:
        """Set the last fetched item ID."""
        self._cur.execute(
            "INSERT INTO current_item(id, item) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET item=?;",
            [current_item, current_item],
        )
        self._cur.fetchall()
        self._con.commit()

    def get_current_item(self) -> int:
        """Get the last fetched item ID."""
        self._cur.execute("SELECT item FROM current_item WHERE id = 1")
        result = self._cur.fetchone()
        return result[0] if result else 27637553


def send_telegram_message(chat_id: int, message: str) -> None:
    """
    Send a message on Telegram to the given chat ID.

    Does not crash, logs to Sentry instead.
    """
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message,
            },
        )
    except Exception as e:
        pprint(e)


def set_webhook(url: str) -> None:
    """Call this function to set the webhook URL."""
    print("Setting webhook...")
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={url}")
    print(r.json())


#############
# Webhook section
#
@app.route(f"/webhook/{WEBHOOK_SECRET}/", methods=["GET", "POST"])
def webhook():
    data = request.get_json()
    message_text = data["message"]["text"].strip()
    chat_id = data["message"]["chat"]["id"]
    if message_text == "/start":
        send_telegram_message(
            chat_id,
            "Welcome! To start receiving replies to your comments on HN,"
            " just tell me your HN username.",
        )
    elif re.search(r"^\w+$", message_text):
        Persistence().set_chat_by_username(message_text, chat_id)
        send_telegram_message(
            chat_id,
            "Okay! From now on, I will send you a message every time "
            f"someone replies to {message_text}.",
        )
    else:
        send_telegram_message(
            chat_id, "Yeah I don't really know what to do with that information."
        )
    return jsonify({})


#############
# Worker section
#
def get_item(item_id: int) -> Optional[Dict[Any, Any]]:
    r = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
    return r.json()


def work():
    p = Persistence()
    max_item = requests.get("https://hacker-news.firebaseio.com/v0/maxitem.json").json()
    current_item = p.get_current_item()
    print(f"Max item is {max_item}.")
    while current_item <= max_item:
        print(f"Getting {current_item}/{max_item}...")
        item = get_item(current_item)
        current_item += 1
        if not item or item["type"] != "comment" or not item.get("text"):
            continue

        pprint(item)

        item_text = item["text"].replace("<p>", "\n\n")
        parent_username = get_item(item["parent"])["by"]
        if chat_id := p.get_chat_by_username(parent_username):
            print("Reply found!")
            send_telegram_message(
                chat_id,
                f"""You have a new reply to this comment: https://news.ycombinator.com/item?id={item['parent']}

The reply by {item['by']} says:

{item_text}

https://news.ycombinator.com/item?id={current_item-1}""",
            )
        p.set_current_item(current_item)
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        while True:
            work()
            time.sleep(30)
    else:
        app.run("0.0.0.0", port=8000, debug=True)

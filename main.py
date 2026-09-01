#!/usr/bin/env python3
import hashlib
import os
import re
import sqlite3
import sys
import time
import traceback
from pprint import pprint
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import requests
from dotenv import load_dotenv
from flask import Flask
from flask import jsonify
from flask import request

app = Flask(__name__)
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:10]
DELAYED_COMMENT_TIMEOUT = 60 * 60

print(f"Webhook URL: /webhook/{WEBHOOK_SECRET}/")


class Persistence:
    """
    The main persistent storage class.

    We currently use three tables: One which stores the last item ID seen (so we can
    process updates for newer things), one which maps a requested username to a chat ID,
    and one which holds delayed replies until their text is available.

    If a chat requests a different username, the old is deleted. If two users request to
    monitor the same username, only one of them wins. This might be a bit of an issue if
    more than three people ever use this.
    """

    def __init__(self, filename: str = "state.sqlite3"):
        self._con = sqlite3.connect(filename)
        self._cur = self._con.cursor()

        self._cur.execute(
            'CREATE TABLE IF NOT EXISTS "user_to_chat" ( "username" TEXT COLLATE NOCASE, "chat_id" INTEGER UNIQUE );'
        )
        self._cur.execute(
            'CREATE TABLE IF NOT EXISTS "current_item" ( "id" INTEGER UNIQUE, "item" INTEGER );'
        )
        self._cur.execute(
            'CREATE TABLE IF NOT EXISTS "pending_comments" ( "comment_id" INTEGER PRIMARY KEY, "parent_id" INTEGER, "chat_id" INTEGER, "first_seen" INTEGER );'
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
            "SELECT chat_id FROM user_to_chat WHERE username = ? COLLATE NOCASE",
            [username],
        )
        result = self._cur.fetchone()
        return result[0] if result else None

    def set_chat_by_username(self, username: str, chat_id: int) -> None:
        """
        Set a user's chat ID for the monitored username.

        If a chat ID already monitors a different username, the old username is
        replaced.
        """
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
        if not result:
            # If there is no current item in the database, it means that this
            # is a new installation. In that case, return the max item, so we
            # don't try to fetch any old comments by default.
            max_item = requests.get(
                "https://hacker-news.firebaseio.com/v0/maxitem.json",
                timeout=60,
            ).json()
            # Save the max item as the current one so we don't go in an
            # infinite loop.
            self.set_current_item(max_item)
            return max_item
        else:
            return result[0]

    def add_pending_comment(
        self, comment_id: int, parent_id: int, chat_id: int
    ) -> None:
        """Add a delayed comment without resetting its original timestamp."""
        self._cur.execute(
            "INSERT OR IGNORE INTO pending_comments(comment_id, parent_id, chat_id, first_seen) "
            "VALUES(?, ?, ?, ?)",
            [comment_id, parent_id, chat_id, int(time.time())],
        )
        self._cur.fetchall()
        self._con.commit()

    def get_pending_comments(self) -> List[Tuple[int, int, int, int]]:
        """Return all delayed comments awaiting their text."""
        self._cur.execute(
            "SELECT comment_id, parent_id, chat_id, first_seen FROM pending_comments"
        )
        return self._cur.fetchall()

    def delete_pending_comment(self, comment_id: int) -> None:
        """Delete a delayed comment by ID."""
        self._cur.execute(
            "DELETE FROM pending_comments WHERE comment_id = ?", [comment_id]
        )
        self._cur.fetchall()
        self._con.commit()

    def delete_expired_pending_comments(self, timeout: int) -> None:
        """Delete delayed comments that have been pending for the timeout."""
        self._cur.execute(
            "DELETE FROM pending_comments WHERE first_seen <= ?",
            [int(time.time()) - timeout],
        )
        self._cur.fetchall()
        self._con.commit()


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
                "parse_mode": "HTML",
            },
            timeout=60,
        )
    except Exception as e:
        pprint(e)


def set_webhook(url: str) -> None:
    """Call this function to set the webhook URL."""
    print("Setting webhook...")
    r = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={url}",
        timeout=60,
    )
    print(r.json())


#############
# Webhook section
#
@app.route(f"/webhook/{WEBHOOK_SECRET}/", methods=["GET", "POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data or "text" not in data["message"]:
        return jsonify({})

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
def get_item(item_id: int, session: requests.Session) -> Optional[Dict[Any, Any]]:
    """Return any HN item."""
    r = session.get(
        f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json",
        timeout=30,
    )
    return r.json()


def is_delayed(text: str) -> bool:
    """Return whether an HN comment is temporarily delayed."""
    return text.strip().lower() == "[delayed]"


def notify_for_reply(
    chat_id: int,
    comment_id: int,
    parent_id: int,
    username: str,
    text: str,
) -> None:
    comment_text = text.replace("<p>", "\n\n")
    send_telegram_message(
        chat_id,
        f"""You have a new reply:

The reply by {username} says:

{comment_text}

Comment: https://news.ycombinator.com/item?id={comment_id}

Parent: https://news.ycombinator.com/item?id={parent_id}""",
    )


def process_comment(
    item: Dict[Any, Any],
    persistence: Persistence,
    session: requests.Session,
) -> None:
    """Process a comment."""
    # Let's see whom they replied to.
    parent_item = get_item(item["parent"], session)
    if not parent_item:
        return

    # Check if the parent asked to be notified.
    parent_username = parent_item.get("by")
    if not parent_username:
        # The parent comment or story may have been deleted.
        return

    chat_id = persistence.get_chat_by_username(parent_username)
    if not chat_id:
        return

    if is_delayed(item["text"]):
        persistence.add_pending_comment(item["id"], item["parent"], chat_id)
        return

    notify_for_reply(
        chat_id,
        comment_id=item["id"],
        parent_id=item["parent"],
        username=item["by"],
        text=item["text"],
    )


def process_pending(persistence: Persistence, session: requests.Session) -> None:
    """Notify for delayed comments whose text has become available."""
    persistence.delete_expired_pending_comments(DELAYED_COMMENT_TIMEOUT)
    pending_comments = persistence.get_pending_comments()
    if not pending_comments:
        return

    current_item = persistence.get_current_item()
    for comment_id, parent_id, chat_id, _ in pending_comments:
        # The scan only advances current_item after a complete pass, so a pass
        # that died partway through will revisit this comment and notify for it
        # itself. Leave those to the scan, otherwise we notify twice.
        if comment_id > current_item:
            continue

        item = get_item(comment_id, session)
        if not item or not item.get("text") or not item.get("by"):
            persistence.delete_pending_comment(comment_id)
            continue

        text = item["text"]
        if is_delayed(text):
            continue

        notify_for_reply(
            chat_id,
            comment_id=comment_id,
            parent_id=parent_id,
            username=item["by"],
            text=text,
        )
        persistence.delete_pending_comment(comment_id)


def work(session: requests.Session) -> None:
    p = Persistence()
    process_pending(p, session)
    max_item = session.get(
        "https://hacker-news.firebaseio.com/v0/maxitem.json",
        timeout=60,
    ).json()
    next_item = p.get_current_item() + 1
    print(f"Max item is {max_item}.")
    last_processed_item = next_item - 1
    while next_item <= max_item:
        print(f"Getting {next_item}/{max_item} ({max_item - next_item} to go)...")
        item = get_item(next_item, session)
        if item and item["type"] == "comment" and item.get("text"):
            process_comment(item, p, session)

        last_processed_item = next_item
        next_item += 1
    print("Done.")
    if last_processed_item >= p.get_current_item():
        p.set_current_item(last_processed_item)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        session = requests.Session()
        while True:
            try:
                work(session)
            except Exception:
                print("There was an exception, will retry later:")
                print(traceback.format_exc())
                # Recreate the session in case it was left in a bad state
                # (e.g., a half-read response from a timeout).
                session = requests.Session()
            time.sleep(30)
    else:
        app.run("0.0.0.0", port=8000)

"""NateSalesBot — B2B sales email automation via Slack (Socket Mode)."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agents import soul_loader
from core.bot import handle_message

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# Slack App (Socket Mode)
app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("message")
def on_message(event, say, client):
    # In DMs, respond to all messages; in channels, ignore (use @mention instead)
    if event.get("channel_type") == "im":
        handle_message(event, say, client)


@app.event("app_mention")
def on_mention(event, say, client):
    handle_message(event, say, client)


if __name__ == "__main__":
    soul_loader.load_all()
    log.info("Digital Sale starting (Socket Mode)...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()

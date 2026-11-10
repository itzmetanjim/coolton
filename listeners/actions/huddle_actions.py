import threading

from slack_sdk import WebClient

from agent import huddle


def handle_huddle_join_approve(ack, body: dict, client: WebClient):
    ack()
    # Joining takes up to a couple of minutes: not on Bolt's listener thread.
    threading.Thread(
        target=huddle.approve, daemon=True, name="huddle-approve",
        args=(client, body["actions"][0].get("value", ""), body.get("user", {}).get("id", "")),
    ).start()


def handle_huddle_join_decline(ack, body: dict, client: WebClient):
    ack()
    huddle.decline(client, body["actions"][0].get("value", ""), body.get("user", {}).get("id", ""))

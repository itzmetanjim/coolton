from slack_sdk import WebClient

from agent import huddle


def handle_huddle_join_approve(ack, body: dict, client: WebClient):
    ack()
    huddle.approve(client, body["actions"][0].get("value", ""), body.get("user", {}).get("id", ""))


def handle_huddle_join_decline(ack, body: dict, client: WebClient):
    ack()
    huddle.decline(client, body["actions"][0].get("value", ""), body.get("user", {}).get("id", ""))

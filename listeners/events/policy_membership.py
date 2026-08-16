from logging import Logger

from slack_sdk import WebClient

from agent.policy_consent import POLICY_CHANNEL_ID, revoke_consent


def handle_member_left_channel(client: WebClient, event: dict, logger: Logger):
    """Leaving #coolton revokes the user's policy consent."""
    if event.get("channel") != POLICY_CHANNEL_ID:
        return
    user_id = event.get("user")
    if not user_id:
        return
    revoke_consent(user_id)
    logger.info("revoked Coolton policy consent after user left policy channel: %s", user_id)

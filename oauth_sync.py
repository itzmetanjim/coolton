import os
import sys
import traceback
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _err_str(exc) -> str:
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            return str(resp.get("error") or resp)
        except Exception:
            return str(resp)
    return str(exc)


def sync_bot_membership(user_token, bot_user_id, dry_run=False):
    from slack_sdk import WebClient

    user = WebClient(token=user_token)
    team_id = os.environ.get("SLACK_TEAM_ID")
    if not team_id:
        auth_team = (user.auth_test() or {}).get("team_id") or ""
        team_id = auth_team if auth_team.startswith("T") else ""
    added, skipped, failed = [], [], []
    types = ["public_channel", "private_channel", "mpim"]
    cursor = None
    while True:
        resp = None
        for attempt in range(3):
            try:
                from slack_sdk.errors import SlackApiError

                resp = user.users_conversations(
                    types=",".join(types),
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                    team_id=team_id,
                )
                break
            except SlackApiError as e:
                err = (e.response or {}).get("error")
                needed = (e.response or {}).get("needed", "")
                drop = {"mpim:read": "mpim"}.get(needed)
                if err == "missing_scope" and drop in types:
                    types.remove(drop)
                    continue
                raise
        if resp is None or not resp.get("ok"):
            failed.append(f"users_conversations: {(resp or {}).get('error') or 'no response'}")
            break
        for ch in resp.get("channels") or []:
            cid = ch["id"]
            name = ch.get("name") or cid
            if ch.get("is_im"):
                continue
            if dry_run:
                added.append(f"{cid} ({name})")
                continue
            try:
                user.conversations_invite(channel=cid, users=bot_user_id)
                added.append(f"{cid} ({name})")
            except Exception as e:
                skipped.append(f"{cid} ({name}): {_err_str(e)}")
        metadata = resp.get("response_metadata") or {}
        cursor = metadata.get("next_cursor")
        if not cursor:
            break
    return {"added": added, "skipped": skipped, "failed": failed}


def main():
    dry_run = "--dry-run" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    bot_user_id = args[0] if args else ""
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
        user_token = os.environ["SLACK_USER_TOKEN"]
        bot_user_id = bot_user_id or os.environ.get("COOLTON_BOT_ID", "")
        summary = sync_bot_membership(user_token, bot_user_id, dry_run=dry_run)
        verb = "would add" if dry_run else "added"
        print(
            f"membership sync ({'dry-run' if dry_run else 'live'}): "
            f"{verb}={len(summary['added'])} skipped={len(summary['skipped'])} failed={len(summary['failed'])}"
        )
        for item in summary["added"]:
            print(f"  add: {item}")
        for item in summary["skipped"]:
            print(f"  skip: {item}")
        for item in summary["failed"]:
            print(f"  fail: {item}")
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()

from agent.tools import human_control


def handle_human_control_done(ack, body: dict, respond):
    ack()
    channel_id, _, thread_ts = body["actions"][0].get("value", "").partition("|")
    if human_control.done(channel_id, thread_ts):
        text = "thanks! coolton is checking the page and will pick it back up."
    else:
        text = "that link has already ended, so there's nothing left to do here."
    respond(text=text, response_type="ephemeral", replace_original=False)

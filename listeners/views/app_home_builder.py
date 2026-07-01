from datetime import datetime


def build_app_home_view(
    install_url: str | None = None,
    is_connected: bool = False,
    endpoints: list[dict] | None = None,
    text_endpoint_id: str | None = None,
    image_endpoint_id: str | None = None,
    has_instructions: bool = False,
    reminders: list[dict] | None = None,
) -> dict:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "coolton"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Send me a *DM* or *mention me in a channel* to get started. I can search the web, analyze images, generate diagrams, run code in a sandbox, schedule reminders, and more.",
            },
        },
        {"type": "divider"},
    ]

    # BYOK section
    endpoints = endpoints or []
    ep_count = len(endpoints)

    if ep_count == 0:
        byok_status = "not configured"
    else:
        byok_status = f"{ep_count} endpoint{'s' if ep_count != 1 else ''} configured"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*BYOK — Bring Your Own Key*\n{byok_status}. Use your own OpenAI-compatible endpoints instead of the global key."},
    })

    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Add Endpoint", "emoji": True}, "action_id": "byok_add"},
        ],
    })

    # List each endpoint
    for ep in endpoints:
        ep_id = ep["id"]
        name = ep["name"]
        model = ep["model"]
        base_url = ep["base_url"]
        is_text = ep_id == text_endpoint_id
        is_image = ep_id == image_endpoint_id
        tags = []
        if is_text:
            tags.append("text")
        if is_image:
            tags.append("image")
        tag_str = f" _[{', '.join(tags)}]_" if tags else ""

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{name}* — `{model}`{tag_str}\n`{base_url}`"},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Edit"}, "action_id": f"byok_edit_{ep_id}"},
                {"type": "button", "text": {"type": "plain_text", "text": "Delete", "emoji": True}, "action_id": f"byok_delete_{ep_id}"},
            ],
        })

    if ep_count > 0:
        blocks.append({"type": "divider"})

    # TEXT default dropdown
    text_options = [{"text": {"type": "plain_text", "text": "Use global key (no BYOK)"}, "value": "none"}]
    text_initial = "none"
    for ep in endpoints:
        opt = {"text": {"type": "plain_text", "text": f"{ep['name']} — {ep['model']}"}, "value": ep["id"]}
        text_options.append(opt)
        if ep["id"] == text_endpoint_id:
            text_initial = ep["id"]

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Default text model:*"},
        "accessory": {
            "type": "static_select",
            "action_id": "byok_select_text",
            "options": text_options,
            "initial_option": next((o for o in text_options if o["value"] == text_initial), text_options[0]),
        },
    })

    # IMAGE default dropdown
    image_options = [{"text": {"type": "plain_text", "text": "Disable image generation"}, "value": "none"}]
    image_initial = "none"
    for ep in endpoints:
        opt = {"text": {"type": "plain_text", "text": f"{ep['name']} — {ep['model']}"}, "value": ep["id"]}
        image_options.append(opt)
        if ep["id"] == image_endpoint_id:
            image_initial = ep["id"]

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Default image model:*"},
        "accessory": {
            "type": "static_select",
            "action_id": "byok_select_image",
            "options": image_options,
            "initial_option": next((o for o in image_options if o["value"] == image_initial), image_options[0]),
        },
    })

    blocks.append({"type": "divider"})

    # REMINDERS section
    reminders = reminders or []
    pending_reminders = [r for r in reminders if not r.get("sent", False)]
    
    if not pending_reminders:
        reminders_text = "No pending reminders."
    else:
        reminders_text = f"*{len(pending_reminders)} pending reminder{'s' if len(pending_reminders) != 1 else ''}:*\n"
        for r in pending_reminders[:5]:
            due = datetime.fromtimestamp(r["due_at"]).strftime("%b %d %H:%M")
            text_preview = r["text"][:80] + ("..." if len(r["text"]) > 80 else "")
            reminders_text += f"• `#{r['id']}` — {text_preview} (due {due})\n"
        if len(pending_reminders) > 5:
            reminders_text += f"  _...and {len(pending_reminders) - 5} more_"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*⏰ Reminders*\n{reminders_text}"},
    })
    
    blocks.append({
        "type": "actions",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Set Reminder", "emoji": True}, "action_id": "reminder_open"},
        ],
    })

    blocks.append({"type": "divider"})

    # MCP section
    if is_connected:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🟢 *Slack MCP Server is connected.*"},
        })
    elif install_url:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"🔴 *Slack MCP Server is disconnected.* <{install_url}|Connect the Slack MCP Server.>"},
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🔴 *Slack MCP Server is disconnected.*"},
        })

    return {"type": "home", "blocks": blocks}
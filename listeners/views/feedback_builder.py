from slack_sdk.models.blocks import (
    Block,
    ContextActionsBlock,
    ContextBlock,
    FeedbackButtonObject,
    FeedbackButtonsElement,
    MarkdownTextObject,
)


def build_feedback_blocks(footer: str = "") -> list[Block]:
    """Build feedback blocks with thumbs up/down buttons, preceded by a small
    `footer` line (e.g. "_done in 12.3s_") when one is given."""
    footer_blocks: list[Block] = [ContextBlock(elements=[MarkdownTextObject(text=footer)])] if footer else []
    return [
        *footer_blocks,
        ContextActionsBlock(
            elements=[
                FeedbackButtonsElement(
                    action_id="feedback",
                    positive_button=FeedbackButtonObject(
                        text="Good Response",
                        accessibility_label="Submit positive feedback on this response",
                        value="good-feedback",
                    ),
                    negative_button=FeedbackButtonObject(
                        text="Bad Response",
                        accessibility_label="Submit negative feedback on this response",
                        value="bad-feedback",
                    ),
                )
            ]
        )
    ]

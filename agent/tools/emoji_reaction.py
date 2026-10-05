import random

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.surface import get_surface as _surface

EMOJI_DESCRIPTION = """\
Add an emoji reaction to the user's current message to acknowledge the topic.

Use any standard Slack emoji that matches the topic or tone of the message. \
Be creative and specific — if someone mentions a dog, use `dog`; if they sound \
frustrated, use `sweat_smile`. The examples below are common picks, not the full set:
- Gratitude/praise: pray, bow, blush, sparkles, star-struck, heart
- Frustration/confusion: thinking_face, face_with_monocle, sweat_smile, upside_down_face
- Something broken: wrench, hammer_and_wrench, mag
- Performance/slow: hourglass_flowing_sand, snail
- Urgency: rotating_light, zap, fire
- Success/celebration: tada, raised_hands, partying_face, rocket, muscle
- Setup/config: gear, package
- Network/connectivity: satellite, signal_strength
- Agreement/acknowledgment: thumbsup, ok_hand, saluting_face, +1
\
"""


async def add_emoji_reaction(
    ctx: RunContext[AgentDeps],
    emoji_name: str,
    force: bool = False,
) -> str:
    """Add an emoji reaction to the user's current message to acknowledge the topic.

    Use any standard Slack emoji that matches the topic or tone of the message.
    Be creative and specific, if someone mentions a dog, use `dog`; if they sound
    frustrated, use `sweat_smile`. The examples below are common picks, not the full set:
    - Gratitude/praise: pray, bow, blush, sparkles, star-struck, heart
    - Frustration/confusion: thinking_face, face_with_monocle, sweat_smile, upside_down_face
    - Something broken: wrench, hammer_and_wrench, mag
    - Performance/slow: hourglass_flowing_sand, snail
    - Urgency: rotating_light, zap, fire
    - Success/celebration: tada, raised_hands, partying_face, rocket, muscle
    - Setup/config: gear, package
    - Network/connectivity: satellite, signal_strength
    - Agreement/acknowledgment: thumbsup, ok_hand, saluting_face, +1

    Call this AT MOST ONCE per turn. Whatever it returns, "Reacted with ...",
    "Already reacted with ...", or "Skipped ...", that's a completed call either
    way; do not call it again to retry, try a different emoji, or "fix" the result.
    Move straight on to the actual task.

    Args:
        ctx: The run context with dependencies.
        emoji_name: The Slack emoji name without colons (e.g. 'tada', 'wrench', 'pray').
        force: If True, disables the 15% chance to not react to avoid over-reacting.
    """
    # One reaction per turn, enforced here: deep into a long turn the model forgets it
    # already reacted, and text_only_response (which reacts too) can end a turn that did.
    deps = ctx.deps
    if getattr(deps, "reacted_with", ""):
        return (f"Already reacted to this message this turn ({deps.reacted_with}); "
                "don't react again, carry on.")
    # Skip ~15% of reactions to feel more natural
    if random.random() < 0.15 and not force:
        deps.reacted_with = "nothing (skipped)"
        return (
            f"Skipped :{emoji_name}: reaction (randomly omitted to avoid over-reacting)"
        )

    deps.reacted_with = f":{emoji_name}:"
    return _surface(deps).react(emoji_name)

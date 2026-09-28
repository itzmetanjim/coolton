"""Cap how many images a single model request carries.

Screenshots from computer_use / see_image_from_sandbox and user-attached
images all stay in a thread's history, so a long computer-use session keeps
re-sending dozens of them on every request — and some models reject a
request outright past a fixed limit (e.g. GLM's 8 images / 64 MiB, a
non-retryable 400). cap_images keeps only the newest MAX_CONTEXT_IMAGES
(within MAX_CONTEXT_IMAGE_BYTES) and swaps older ones for a short text note.

Used as a pydantic-ai ProcessHistory capability, which only changes what is
sent in each request: the stored thread history keeps every image, and the
original message objects are never mutated.
"""
from __future__ import annotations

from dataclasses import replace

from pydantic_ai.messages import BinaryContent, ImageUrl

MAX_CONTEXT_IMAGES = 8
MAX_CONTEXT_IMAGE_BYTES = 60 * 1024 * 1024
OMITTED_NOTE = f"[image omitted: only the {MAX_CONTEXT_IMAGES} most recent images are kept in context]"


def _is_image(item) -> bool:
    if isinstance(item, ImageUrl):
        return True
    return isinstance(item, BinaryContent) and (item.media_type or "").startswith("image/")


def _size(item) -> int:
    return len(item.data) if isinstance(item, BinaryContent) else 0


def _items(content) -> list | None:
    """A part's content as a list of items, or None if it holds no media."""
    if isinstance(content, list):
        return content
    if _is_image(content):
        return [content]
    return None


def cap_images(messages: list) -> list:
    # Newest-first pass: decide which images survive.
    keep: set[tuple[int, int, int]] = set()
    kept_bytes = 0
    total = 0
    for mi in range(len(messages) - 1, -1, -1):
        parts = getattr(messages[mi], "parts", [])
        for pi in range(len(parts) - 1, -1, -1):
            items = _items(getattr(parts[pi], "content", None))
            if not items:
                continue
            for ii in range(len(items) - 1, -1, -1):
                if not _is_image(items[ii]):
                    continue
                total += 1
                size = _size(items[ii])
                if len(keep) < MAX_CONTEXT_IMAGES and kept_bytes + size <= MAX_CONTEXT_IMAGE_BYTES:
                    keep.add((mi, pi, ii))
                    kept_bytes += size
    if total == len(keep):
        return messages

    # Rebuild only the messages that lose an image, as copies.
    result = list(messages)
    for mi, message in enumerate(messages):
        parts = list(getattr(message, "parts", []))
        changed = False
        for pi, part in enumerate(parts):
            content = getattr(part, "content", None)
            items = _items(content)
            if not items:
                continue
            new_items = [
                OMITTED_NOTE if _is_image(item) and (mi, pi, ii) not in keep else item
                for ii, item in enumerate(items)
            ]
            if new_items != items:
                new_content = new_items if isinstance(content, list) else new_items[0]
                parts[pi] = replace(part, content=new_content)
                changed = True
        if changed:
            result[mi] = replace(message, parts=parts)
    return result

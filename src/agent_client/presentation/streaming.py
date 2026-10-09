from dataclasses import dataclass
from math import ceil
from unicodedata import combining


@dataclass(frozen=True)
class StreamRendering:
    frames_per_second: int = 60
    reveal_seconds: float = 0.3
    minimum_characters: int = 8
    skin_tone_first: int = 0x1F3FB
    skin_tone_last: int = 0x1F3FF
    regional_first: int = 0x1F1E6
    regional_last: int = 0x1F1FF
    regional_pair_size: int = 2


DEFAULT_STREAM_RENDERING = StreamRendering()
FRAME_SECONDS = 1 / DEFAULT_STREAM_RENDERING.frames_per_second
REVEAL_SECONDS = DEFAULT_STREAM_RENDERING.reveal_seconds


def reveal_prefix(
    target: str, rendered: str, *, animate: bool, remaining_seconds: float, render_seconds: float
) -> str:
    if not animate or not target.startswith(rendered) or remaining_seconds <= 0:
        return target
    pending = len(target) - len(rendered)
    frames = max(1, int(remaining_seconds / max(FRAME_SECONDS, render_seconds)))
    end = min(
        len(target),
        len(rendered) + max(DEFAULT_STREAM_RENDERING.minimum_characters, ceil(pending / frames)),
    )
    while end < len(target):
        following = ord(target[end])
        if (
            combining(target[end])
            or target[end] in {"\u200d", "\ufe0f", "\ufe0e"}
            or target[end - 1] == "\u200d"
            or DEFAULT_STREAM_RENDERING.skin_tone_first
            <= following
            <= DEFAULT_STREAM_RENDERING.skin_tone_last
            or (target[end - 1] == "\r" and target[end] == "\n")
        ):
            end += 1
            continue
        if (
            DEFAULT_STREAM_RENDERING.regional_first
            <= following
            <= DEFAULT_STREAM_RENDERING.regional_last
        ):
            start = end
            while (
                start
                and DEFAULT_STREAM_RENDERING.regional_first
                <= ord(target[start - 1])
                <= DEFAULT_STREAM_RENDERING.regional_last
            ):
                start -= 1
            if (end - start) % DEFAULT_STREAM_RENDERING.regional_pair_size:
                end += 1
                continue
        break
    return target[:end]

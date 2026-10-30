"""Dependency-free delivery-mode configuration shared by server and tests."""

import os


STREAMING_MODES = ("turn", "realtime")


def parse_streaming_mode(value: str | None) -> str:
    mode = (value or "turn").strip().lower()
    if mode not in STREAMING_MODES:
        raise ValueError(
            f"invalid streaming mode {value!r}; expected {' or '.join(STREAMING_MODES)}"
        )
    return mode


def default_streaming_mode() -> str:
    return parse_streaming_mode(os.environ.get("STREAMING_MODE"))

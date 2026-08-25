"""HTTP Range header parsing for the video streaming endpoint.

Kept separate from the route so the parsing rules can be unit-tested directly:
this is the part of a streaming server that clients exercise hardest, and the
previous implementation clamped nothing.
"""

from __future__ import annotations


class RangeNotSatisfiable(Exception):
    """The requested range cannot be served; the caller should return 416."""


def parse_range(header: str | None, file_size: int) -> tuple[int, int] | None:
    """Resolve a Range header to inclusive byte offsets.

    Returns ``None`` when the whole entity should be sent (no header, an
    unsupported unit, a malformed value, or a multi-range request, which this
    server does not implement). Raises ``RangeNotSatisfiable`` when the header
    is well-formed but cannot be satisfied.
    """
    if not header:
        return None

    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or not spec.strip():
        return None

    # Multipart ranges would need a multipart/byteranges body; fall back to 200.
    if "," in spec:
        return None

    start_text, separator, end_text = spec.strip().partition("-")
    if not separator:
        return None

    if not start_text:
        # Suffix form: "bytes=-500" means the final 500 bytes.
        try:
            suffix = int(end_text)
        except ValueError:
            return None
        if suffix <= 0 or file_size == 0:
            raise RangeNotSatisfiable(header)
        return max(0, file_size - suffix), file_size - 1

    try:
        start = int(start_text)
    except ValueError:
        return None

    if start < 0:
        return None
    # Checked before deriving the default end, which would otherwise be -1 for
    # an empty file and fall through the negative guard below.
    if file_size == 0 or start >= file_size:
        raise RangeNotSatisfiable(header)

    try:
        end = int(end_text) if end_text else file_size - 1
    except ValueError:
        return None

    if end < 0:
        return None
    if start > end:
        raise RangeNotSatisfiable(header)

    # A client may ask for more than exists; the response must describe what is
    # actually sent, so the upper bound is clamped to the last byte.
    return start, min(end, file_size - 1)

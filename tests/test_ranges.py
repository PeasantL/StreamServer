"""Range header parsing - the part clients exercise hardest when seeking."""

import pytest

from ranges import RangeNotSatisfiable, parse_range

SIZE = 1000


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("bytes=0-", (0, SIZE - 1)),
        ("bytes=0-0", (0, 0)),
        ("bytes=500-600", (500, 600)),
        ("BYTES=0-10", (0, 10)),
        ("bytes=-500", (500, 999)),
        # A suffix longer than the file is clamped to the whole file.
        ("bytes=-99999", (0, 999)),
        # The regression this endpoint most needed: an end past EOF must be
        # clamped, or Content-Length promises more than the body delivers.
        ("bytes=0-99999", (0, SIZE - 1)),
        ("bytes=900-99999", (900, SIZE - 1)),
    ],
)
def test_valid_ranges(header, expected):
    assert parse_range(header, SIZE) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=1000-",       # start at EOF
        "bytes=2000-3000",   # entirely past EOF
        "bytes=600-500",     # inverted
        "bytes=-0",          # zero-length suffix
    ],
)
def test_unsatisfiable_ranges(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, SIZE)


@pytest.mark.parametrize(
    "header",
    [
        "items=0-10",        # unsupported unit
        "bytes=abc",         # not a number
        "bytes=0--5",        # malformed
        "bytes=",            # empty spec
        "bytes=0-10,20-30",  # multipart, which this server does not implement
    ],
)
def test_ignored_headers_fall_back_to_whole_entity(header):
    assert parse_range(header, SIZE) is None


def test_empty_file_is_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-", 0)
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=-10", 0)

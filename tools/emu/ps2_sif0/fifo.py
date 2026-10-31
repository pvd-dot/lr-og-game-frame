"""Source-shaped 580-byte SIF0 FIFO ring and word helpers."""

from __future__ import annotations

import struct
from typing import Any

SIF_BYTES, FIFO_WORDS = 0x244, 128
FIFO_DATA, FIFO_JUNK, FIFO_READ, FIFO_WRITE, FIFO_SIZE = 0, 0x200, 0x210, 0x214, 0x218


class Refusal(ValueError):
    pass


def _u32(buf: bytearray, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _put32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<I", buf, off, value & 0xFFFFFFFF)


def _s32(buf: bytearray, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _puts32(buf: bytearray, off: int, value: int) -> None:
    struct.pack_into("<i", buf, off, value)


def write_junk(sif: bytearray, words: int,
               fifo_origin: list[tuple[str, int] | None] | None = None) -> None:
    """sifFifo::writeJunk, including serialized scratch and wrapped copies."""
    if words <= 0:
        return
    if len(sif) != SIF_BYTES or not 1 <= words <= 3:
        raise Refusal("writeJunk requires a 580-byte channel and 1..3 words")
    write_pos, size = _s32(sif, FIFO_WRITE), _s32(sif, FIFO_SIZE)
    if not 0 <= write_pos < FIFO_WORDS or not 0 <= size <= FIFO_WORDS - words:
        raise Refusal("invalid/full FIFO state for writeJunk")

    # Sif.h: prevQWPos = (writePos - (4 + (4-words))) & 127.
    prev_qw = (write_pos - (8 - words)) & (FIFO_WORDS - 1)
    prior = [_u32(sif, FIFO_DATA + ((prev_qw + i) & 127) * 4) for i in range(4)]
    prior_origin = ([fifo_origin[(prev_qw + i) & 127] for i in range(4)]
                    if fifo_origin is not None else None)
    for i, value in enumerate(prior):
        _put32(sif, FIFO_JUNK + i * 4, value)

    w0 = min(FIFO_WORDS - write_pos, words)
    w1 = words - w0
    for i in range(w0):
        src_i = 4 - w0 + i
        dst = (write_pos + i) & (FIFO_WORDS - 1)
        _put32(sif, FIFO_DATA + dst * 4, prior[src_i])
        if fifo_origin is not None:
            fifo_origin[dst] = prior_origin[src_i]
    for i in range(w1):
        # Source memcpy is junk[wP0]; the source index is global loop index i.
        src_i, dst = w0 + i, i
        _put32(sif, FIFO_DATA + dst * 4, prior[src_i])
        if fifo_origin is not None:
            fifo_origin[dst] = prior_origin[src_i]

    _puts32(sif, FIFO_WRITE, (write_pos + words) & (FIFO_WORDS - 1))
    _puts32(sif, FIFO_SIZE, size + words)

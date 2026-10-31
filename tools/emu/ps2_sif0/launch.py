"""Experimental PCSX2 v2.8.2 synchronous DMA9/SIF0 launch slice.

This models a bounded source path, not PS2 hardware or the whole emulator.
Only direct IOP/EE RAM addresses and linear IOP tag chains are supported.
Source pins and the state boundary are in docs/consoles/ps2-functions.md.
"""

from __future__ import annotations

import struct
from typing import Any

from .fifo import (FIFO_DATA, FIFO_JUNK, FIFO_READ, FIFO_SIZE, FIFO_WORDS,
                       FIFO_WRITE, SIF_BYTES, Refusal, _put32, _puts32, _s32,
                       _u32, write_junk as _write_junk)
from .scheduler import _schedule_iop
from .state import (
    EE_BUSY, EE_CYCLES, EE_END, EE_RAM_BYTES, HW_BYTES, IOP_BUSY, IOP_COUNTER,
    IOP_CYCLES, IOP_DATA, IOP_DMA9_BCR, IOP_DMA9_CHCR, IOP_DMA9_MADR,
    IOP_DMA9_TADR, IOP_DMA_PCR2, IOP_END, IOP_JUNK,
    IOP_RAM_BYTES, IOP_RAM_MASK, IOP_TAG_HI, IOP_TAG_LO, IOP_WORDS,
    EE_DMAC_CTRL, EE_DMAC_STADR, EE_DMACH_CHCR, EE_DMACH_MADR, EE_DMACH_QWC,
    EE_SBUS_F240, EE_SIF0_CH,
)

def _iop_range(buf: bytearray, addr: int, count: int, label: str) -> tuple[int, int]:
    # IopMem.h::iopPhysMem uses ExposedIopRam-1, pinned here to 2 MiB.
    if len(buf) != IOP_RAM_BYTES:
        raise Refusal("IOP RAM must be exactly the pinned 2 MiB exposed size")
    start = addr & IOP_RAM_MASK
    if count < 0 or count > len(buf) or start + count > len(buf):
        raise Refusal(f"{label}: wrapped or out-of-range span is unsupported")
    return start, start + count


def _ee_range(buf: bytearray, addr: int, count: int, label: str) -> tuple[int, int]:
    # dmaGetAddr supports aliases and scratchpad; this prototype accepts only
    # direct EE RAM offsets and never silently masks an EE destination.
    if len(buf) != EE_RAM_BYTES or count < 0 or addr < 0 or addr + count > EE_RAM_BYTES:
        raise Refusal(f"{label}: not a supported direct EE RAM span")
    return addr, addr + count


def _fifo_write(sif: bytearray, words: list[int]) -> None:
    pos = _s32(sif, FIFO_WRITE)
    size = _s32(sif, FIFO_SIZE)
    if size < 0 or size + len(words) > FIFO_WORDS or not 0 <= pos < FIFO_WORDS:
        raise Refusal("invalid/full SIF0 FIFO write")
    for value in words:
        _put32(sif, FIFO_DATA + pos * 4, value)
        pos = (pos + 1) & (FIFO_WORDS - 1)
    _puts32(sif, FIFO_WRITE, pos)
    _puts32(sif, FIFO_SIZE, size + len(words))


def _fifo_read(sif: bytearray, words: int) -> list[int]:
    pos = _s32(sif, FIFO_READ)
    size = _s32(sif, FIFO_SIZE)
    if size < words or size < 0 or not 0 <= pos < FIFO_WORDS:
        raise Refusal("SIF0 FIFO underflow or invalid cursor")
    out = []
    for _ in range(words):
        out.append(_u32(sif, FIFO_DATA + pos * 4))
        pos = (pos + 1) & (FIFO_WORDS - 1)
    _puts32(sif, FIFO_READ, pos)
    _puts32(sif, FIFO_SIZE, size - words)
    return out


def _write_u32_words(buf: bytearray, addr: int, words: list[int], label: str) -> None:
    start, end = _ee_range(buf, addr, len(words) * 4, label)
    for i, value in enumerate(words):
        _put32(buf, start + i * 4, value)


def launch(
    iop_ram: bytearray,
    iop_hw: bytearray,
    ee_ram: bytearray,
    ee_hw: bytearray,
    sif0: bytearray,
    scheduler: dict[str, Any],
    chcr: int = 0x01000701,
) -> dict[str, Any]:
    """Run the supported live DMA9/SIF0 source path atomically on success.

    Arrays are mutated in place only if the full bounded transaction succeeds.
    Scheduler may add bridge keys iop_cycle_ee, ee_cycle, ee_next_event. The
    returned transfers are discovered from descriptors, never supplied outputs.
    """
    arrays = (iop_ram, iop_hw, ee_ram, ee_hw, sif0)
    if not all(isinstance(x, bytearray) for x in arrays):
        return {"status": "refused", "reason": "memory inputs must be bytearray"}
    if len({id(x) for x in arrays}) != len(arrays):
        return {"status": "refused", "reason": "memory inputs must be distinct bytearrays"}
    if (len(iop_ram) != IOP_RAM_BYTES or len(ee_ram) != EE_RAM_BYTES or
            len(iop_hw) != HW_BYTES or len(ee_hw) != HW_BYTES or len(sif0) != SIF_BYTES):
        return {"status": "refused", "reason": "requires exact 2MiB IOP RAM, 32MiB EE RAM, 64KiB HW arrays, and 580-byte SIF0"}
    if not isinstance(scheduler, dict):
        return {"status": "refused", "reason": "scheduler must be dict"}

    # Copy all mutable inputs before any potentially rejecting work.
    ir, ih, er, eh, sf, sch = (bytearray(iop_ram), bytearray(iop_hw), bytearray(ee_ram),
                               bytearray(ee_hw), bytearray(sif0), dict(scheduler))
    try:
        if type(chcr) is not int or chcr < 0 or chcr > 0xFFFFFFFF:
            raise Refusal("CHCR value is not u32")
        _put32(ih, IOP_DMA9_CHCR, chcr)
        if not (chcr & 0x01000000):
            raise Refusal("start-clear CHCR is outside the modeled launch domain")
        if not (_u32(ih, IOP_DMA_PCR2) & 0x800):
            raise Refusal("DMA9 PCR2-disabled path is outside the modeled launch domain")

        for off in (FIFO_READ, FIFO_WRITE):
            if not 0 <= _s32(sf, off) < FIFO_WORDS:
                raise Refusal("invalid SIF0 FIFO cursor")
        fifo_size = _s32(sf, FIFO_SIZE)
        if not 0 <= fifo_size <= FIFO_WORDS:
            raise Refusal("invalid SIF0 FIFO occupancy")
        if fifo_size != 0 or _s32(sf, IOP_COUNTER) != 0 or _s32(sf, IOP_JUNK) != 0 or sf[IOP_BUSY]:
            raise Refusal("only the captured empty-FIFO, idle-IOP launch boundary is supported")
        if any(sf[off] not in (0, 1) for off in (EE_END, EE_BUSY, IOP_END, IOP_BUSY)):
            raise Refusal("serialized SIF0 end/busy fields must be boolean bytes")
        ee_chcr = _u32(eh, EE_SIF0_CH + EE_DMACH_CHCR)
        if not sf[EE_BUSY] or sf[EE_END] or not (ee_chcr & 0x100) or ((ee_chcr >> 2) & 3) != 1:
            raise Refusal("requires live EE SIF0 busy chain-mode state with STR set and end clear")
        _puts32(sf, EE_CYCLES, 0)
        _puts32(sf, IOP_CYCLES, 0)
        # psxDma9 enters SIF0Dma directly. The EE channel was armed by its
        # caller; this path does not call dmaSIF0 or rewrite EE busy/end state.
        sf[IOP_BUSY], sf[IOP_END] = 1, 0

        transfers: list[dict[str, int]] = []
        seen_tags: set[int] = set()
        source_descriptor: dict[int, int] = {}
        fifo_origin: list[tuple[str, int] | None] = [None] * FIFO_WORDS
        loops = 0
        while True:
            loops += 1
            if loops > 4096:
                raise Refusal("descriptor loop exceeds model bound")
            progress = False

            # Exact SIF0Dma ordering: junk, IOP handler, then EE handler.
            counter = _s32(sf, IOP_COUNTER)
            junk = _s32(sf, IOP_JUNK)
            if counter == 0 and junk and FIFO_WORDS - _s32(sf, FIFO_SIZE) >= junk:
                # Implement source writeJunk ring semantics, preserving its temp.
                if not 1 <= junk <= 3:
                    raise Refusal("writeJunk outside source 1..3 range")
                _write_junk(sf, junk, fifo_origin)
                _puts32(sf, IOP_JUNK, 0)

            if sf[IOP_BUSY] and (FIFO_WORDS - _s32(sf, FIFO_SIZE) > 0 or (sf[IOP_END] and _s32(sf, IOP_COUNTER) == 0)):
                progress = True
                if _s32(sf, IOP_COUNTER) <= 0:
                    if sf[IOP_END]:
                        sf[IOP_END], sf[IOP_BUSY] = 0, 0
                        _put32(sf, IOP_DATA, 0)
                        cycles = _s32(sf, IOP_CYCLES)
                        if cycles == 0:
                            cycles = 1
                            _puts32(sf, IOP_CYCLES, cycles)
                        if cycles > 1000:
                            cycles >>= 1
                            _puts32(sf, IOP_CYCLES, cycles)
                        _schedule_iop(sch, cycles)
                    else:
                        tadr = _u32(ih, IOP_DMA9_TADR)
                        if tadr in seen_tags:
                            raise Refusal("cyclic IOP tag chain unsupported")
                        seen_tags.add(tadr)
                        tag_at, tag_end = _iop_range(ir, tadr, 24, "IOP tag and embedded FIFO words")
                        source, words, ee_tag0, ee_tag1 = struct.unpack_from("<IIII", ir, tag_at)
                        _put32(sf, IOP_DATA, source)
                        _put32(sf, IOP_WORDS, words)
                        _put32(sf, IOP_TAG_LO, ee_tag0)
                        _put32(sf, IOP_TAG_HI, ee_tag1)
                        _fifo_write(sf, [ee_tag0, ee_tag1, _u32(ir, tag_at + 16), _u32(ir, tag_at + 20)])
                        wp = (_s32(sf, FIFO_WRITE) - 4) & 127
                        for i in range(4):
                            fifo_origin[(wp + i) & 127] = ("tag", tadr + 8 + i * 4 if i < 2 else tadr + 16 + (i - 2) * 4)
                        _put32(ih, IOP_DMA9_TADR, tadr + 16)
                        _put32(ih, IOP_DMA9_MADR, source & 0xFFFFFF)
                        source_descriptor[source & 0xFFFFFF & (len(ir) - 1)] = tadr
                        count = words & 0xFFFFF
                        _puts32(sf, IOP_COUNTER, count)
                        _puts32(sf, IOP_JUNK, ((4 - count) & 3) if count else 0)
                        raw_tag = source
                        sf[IOP_END] = int(bool((raw_tag >> 31) or (((raw_tag >> 28) & 7) & 4)))
                else:
                    n = min(_s32(sf, IOP_COUNTER), FIFO_WORDS - _s32(sf, FIFO_SIZE))
                    source = _u32(ih, IOP_DMA9_MADR)
                    start, end = _iop_range(ir, source, n * 4, "IOP payload")
                    payload = list(struct.unpack_from("<" + "I" * n, ir, start)) if n else []
                    _fifo_write(sf, payload)
                    wp = (_s32(sf, FIFO_WRITE) - n) & 127
                    for i in range(n):
                        fifo_origin[(wp + i) & 127] = ("payload", (source + i * 4) & (len(ir) - 1))
                    _put32(ih, IOP_DMA9_MADR, source + n * 4)
                    _puts32(sf, IOP_CYCLES, _s32(sf, IOP_CYCLES) + n)
                    _puts32(sf, IOP_COUNTER, _s32(sf, IOP_COUNTER) - n)

            if sf[EE_BUSY] and (_s32(sf, FIFO_SIZE) >= 4 or (sf[EE_END] and _u32(eh, EE_SIF0_CH + EE_DMACH_QWC) == 0)):
                progress = True
                chcr_ee = _u32(eh, EE_SIF0_CH + EE_DMACH_CHCR)
                qwc = _u32(eh, EE_SIF0_CH + EE_DMACH_QWC)
                dmac_stall_sif0 = ((_u32(eh, EE_DMAC_CTRL) >> 4) & 3) == 1
                if not (chcr_ee & 0x100):
                    sf[EE_END], sf[EE_BUSY] = 0, 0
                elif qwc <= 0:
                    mode = (chcr_ee >> 2) & 3
                    if mode == 0 or sf[EE_END]:
                        sf[EE_END], sf[EE_BUSY] = 0, 0
                        cycles = _s32(sf, EE_CYCLES) or 1
                        # EndEE/CPU_INT is outside this captured boundary; reject
                        # if reached rather than fabricate EE interrupt machinery.
                        raise Refusal(f"EE completion path reached (cycles={cycles}); unsupported")
                    elif _s32(sf, FIFO_SIZE) >= 4:
                        tag0, tag1, _, _ = _fifo_read(sf, 4)
                        qwc = tag0 & 0xFFFF
                        _put32(eh, EE_SIF0_CH + EE_DMACH_CHCR, (chcr_ee & 0xFFFF) | (tag0 & 0xFFFF0000))
                        _put32(eh, EE_SIF0_CH + EE_DMACH_MADR, tag1)
                        _put32(eh, EE_SIF0_CH + EE_DMACH_QWC, qwc)
                        tag_id = (tag0 >> 28) & 7
                        if dmac_stall_sif0 and tag_id == 0:  # TAG_CNTS
                            _put32(eh, EE_DMAC_STADR, tag1)
                        if ((chcr_ee >> 7) & 1) and ((tag0 >> 31) & 1):
                            sf[EE_END] = 1
                        if tag_id == 7:
                            sf[EE_END] = 1
                if _u32(eh, EE_SIF0_CH + EE_DMACH_QWC) > 0:
                    qwc = _u32(eh, EE_SIF0_CH + EE_DMACH_QWC)
                    n = min(qwc, _s32(sf, FIFO_SIZE) >> 2)
                    if n:
                        dest = _u32(eh, EE_SIF0_CH + EE_DMACH_MADR)
                        rp = _s32(sf, FIFO_READ)
                        origins = [fifo_origin[(rp + i) & 127] for i in range(n * 4)]
                        payload = _fifo_read(sf, n * 4)
                        _write_u32_words(er, dest, payload, "EE destination")
                        _put32(eh, EE_SIF0_CH + EE_DMACH_MADR, dest + (n << 4))
                        _put32(eh, EE_SIF0_CH + EE_DMACH_QWC, qwc - n)
                        _puts32(sf, EE_CYCLES, _s32(sf, EE_CYCLES) + n)
                        chcr_after = _u32(eh, EE_SIF0_CH + EE_DMACH_CHCR)
                        if (dmac_stall_sif0 and _u32(eh, EE_SIF0_CH + EE_DMACH_QWC) == 0 and
                                (((chcr_after >> 2) & 3) == 0 or ((chcr_after >> 16) & 7) == 0)):
                            _put32(eh, EE_DMAC_STADR, _u32(eh, EE_SIF0_CH + EE_DMACH_MADR))
                        first_source = next((addr for kind, addr in origins if kind == "payload"), -1)
                        transfers.append({"source": first_source, "destination": dest, "length": n * 16,
                                          "iop_tag_address": source_descriptor.get(first_source, -1),
                                          "qwc": n})
            if not progress:
                break

        # SIF0End's direct IOP-HW writes and SBus bit clear.
        _put32(eh, EE_SBUS_F240, _u32(eh, EE_SBUS_F240) & ~0x2020)
        if not transfers:
            raise Refusal("launch completed without any EE payload windows")
        # Commit only after every operation and scheduling step succeeded.
        iop_ram[:] = ir; iop_hw[:] = ih; ee_ram[:] = er; ee_hw[:] = eh; sif0[:] = sf
        scheduler.clear(); scheduler.update(sch)
        return {"status": "completed", "transfers": transfers, "loops": loops,
                "limitations": ["PCSX2 v2.8.2 source-qualified model; not physical hardware",
                                "direct RAM spans only; no EE aliases/scratchpad/host timing",
                                "DMAC STS=SIF0 updates STADR for modeled CNTS/NORMAL cases",
                                "EE EndEE completion and interrupt path unsupported",
                                "only synchronous DMA9/SIF0 launch slice modeled"]}
    except (Refusal, struct.error, IndexError, OverflowError, TypeError, KeyError) as exc:
        return {"status": "refused", "reason": str(exc), "transfers": []}



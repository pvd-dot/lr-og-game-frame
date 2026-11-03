"""One bounded PCSX2 IOP SIF0 due-event callback transition.

This models the selected software callback at an already reached IOP event
test. It does not advance a CPU or model host/device scheduling.
"""

from __future__ import annotations

from typing import Any

from .fifo import Refusal, _put32, _u32
from .state import HW_BYTES, IOP_EVENT_SIF0, IOP_DMA9_CHCR

IOP_ISTAT = 0x1070
IOP_IMASK = 0x1074
IOP_ICTRL = 0x1078
IOP_DMA_ICR2 = 0x1574
IOP_WAIT_CYCLES = 384
DMA9_START = 1 << 24
DMA2_IRQ = 1 << (24 + 2)
DMA_IRQ_MASTER_ENABLE = 1 << 23
DMA_IRQ_MASTER = 1 << 31
IOP_IRQ_DMA = 1 << 3
U64_MAX = (1 << 64) - 1
S32_MAX = (1 << 31) - 1


def _word(buf: bytearray, offset: int) -> int:
    return _u32(buf, offset)


def complete_due_sif0(
    iop_hw: bytearray,
    scheduler: dict[str, Any],
    *,
    counter_deadline: int,
) -> dict[str, str]:
    """Apply one due SIF0 callback and source-shaped event reschedule.

    `scheduler` uses the SIF0-launch fields `cycle`, `interrupt`,
    `event_start`, `event_delta`, and `next_event`. The caller supplies the
    independently maintained absolute counter deadline. The operation
    mutates hardware and scheduler only when the SIF0 event is due and is the
    sole pending event, no counter is due, ICTRL suppresses interrupt entry,
    and all native cycle arithmetic stays in the supported domain.

    A successful call clears DMA9 CHCR.TR, raises ICR2 channel-2 completion
    (and the master IRQ bit when enabled), raises I_STAT bit 3, clears the
    SIF0 pending event, and chooses the next event as the earlier of the
    384-cycle default horizon and counter deadline. It models PCSX2 software
    effects only; it does not execute guest instructions or advance time.
    """
    if not isinstance(iop_hw, bytearray) or len(iop_hw) != HW_BYTES:
        return {"status": "refused", "reason": "IOP hardware must be an exact 64 KiB bytearray"}
    if not isinstance(scheduler, dict):
        return {"status": "refused", "reason": "scheduler must be a dict"}

    required = ("cycle", "interrupt", "event_start", "event_delta", "next_event")
    if any(key not in scheduler for key in required):
        return {"status": "refused", "reason": "scheduler is missing a required SIF0 event field"}

    # Stage every changed object so every unsupported path is atomic.
    hw = bytearray(iop_hw)
    staged = dict(scheduler)
    try:
        cycle = _u64(staged["cycle"], "cycle")
        interrupt = _u32_int(staged["interrupt"], "interrupt")
        start = _u64(staged["event_start"], "event_start")
        delta = _s32_int(staged["event_delta"], "event_delta")
        old_next = _u64(staged["next_event"], "next_event")
        deadline = _u64(counter_deadline, "counter_deadline")

        if interrupt != (1 << IOP_EVENT_SIF0):
            raise Refusal("requires the SIF0 event as the sole pending IOP event")
        if delta < 0:
            raise Refusal("negative SIF0 event delay is outside this due-event profile")
        if cycle < start:
            raise Refusal("SIF0 event start is ahead of the current cycle")
        elapsed = cycle - start
        if elapsed > S32_MAX:
            raise Refusal("SIF0 elapsed-cycle narrowing is outside the modeled s32 domain")
        if start + delta > U64_MAX:
            raise Refusal("SIF0 due-cycle addition overflows u64")
        if elapsed < delta:
            return {"status": "not_due", "reason": "SIF0 event has not reached its source due check"}
        due_cycle = start + delta
        if old_next != due_cycle:
            raise Refusal("sole-event scheduler next cycle does not identify the SIF0 due check")
        if deadline <= cycle:
            raise Refusal("counter deadline is due at or before the SIF0 callback")
        counter_wait = deadline - cycle
        if counter_wait > S32_MAX:
            raise Refusal("counter wait is outside the source signed-cycle domain")
        if cycle > U64_MAX - IOP_WAIT_CYCLES:
            raise Refusal("default IOP event horizon overflows u64")

        # In R3000A.cpp iopEventTest first assigns cycle + iopWaitCycles,
        # then replaces it only when the counter delta is strictly smaller.
        next_event = cycle + IOP_WAIT_CYCLES
        if counter_wait < IOP_WAIT_CYCLES:
            next_event = deadline

        ictrl = _word(hw, IOP_ICTRL)
        if ictrl != 0:
            raise Refusal("nonzero ICTRL can enter IRQ scheduling/exception paths")

        chcr = _word(hw, IOP_DMA9_CHCR)
        icr2 = _word(hw, IOP_DMA_ICR2)
        istat = _word(hw, IOP_ISTAT)

        _put32(hw, IOP_DMA9_CHCR, chcr & ~DMA9_START)
        icr2 |= DMA2_IRQ
        if icr2 & DMA_IRQ_MASTER_ENABLE:
            icr2 |= DMA_IRQ_MASTER
        _put32(hw, IOP_DMA_ICR2, icr2)
        _put32(hw, IOP_ISTAT, istat | IOP_IRQ_DMA)

        staged["interrupt"] = interrupt & ~(1 << IOP_EVENT_SIF0)
        staged["next_event"] = next_event
    except (Refusal, TypeError, KeyError, OverflowError) as exc:
        return {"status": "refused", "reason": str(exc)}

    iop_hw[:] = hw
    scheduler.clear()
    scheduler.update(staged)
    return {"status": "completed", "reason": ""}


def _u32_int(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise Refusal(f"{label} is outside u32")
    return value


def _u64(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= U64_MAX:
        raise Refusal(f"{label} is outside u64")
    return value


def _s32_int(value: object, label: str) -> int:
    if type(value) is not int or not -(1 << 31) <= value <= S32_MAX:
        raise Refusal(f"{label} is outside s32")
    return value

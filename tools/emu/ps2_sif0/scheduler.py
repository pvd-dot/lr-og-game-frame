"""Bounded PCSX2 IOP/EE scheduler updates used by the SIF0 launch model."""

from __future__ import annotations

import ctypes
from typing import Any

from .fifo import Refusal
from .state import IOP_EVENT_SIF0


def _native_int(values: dict[str, Any], key: str, lo: int, hi: int) -> int:
    value = values[key]
    if type(value) is not int or not lo <= value <= hi:
        raise Refusal(f"scheduler {key} outside native integer domain [{lo}, {hi}]")
    return value


def _schedule_iop(scheduler: dict[str, Any], delay: int) -> None:
    """Source-shaped PSX_INT + psxSetNextBranchDelta + EE horizon update."""
    required = ("cycle", "interrupt", "event_start", "event_delta", "next_event")
    if any(k not in scheduler for k in required):
        raise Refusal("scheduler requires cycle, interrupt, event_start, event_delta, next_event")
    if not 0 <= delay <= 0x7FFFFFFF:
        raise Refusal("IOP event delay outside signed-positive source domain")
    cycle = _native_int(scheduler, "cycle", 0, 0xFFFFFFFFFFFFFFFF)
    _native_int(scheduler, "interrupt", 0, 0xFFFFFFFF)
    _native_int(scheduler, "event_start", 0, 0xFFFFFFFFFFFFFFFF)
    _native_int(scheduler, "event_delta", -0x80000000, 0x7FFFFFFF)
    next_iop = _native_int(scheduler, "next_event", 0, 0xFFFFFFFFFFFFFFFF)
    if next_iop < cycle:
        raise Refusal("next_event precedes cycle; unsigned-to-signed source conversion is out of model domain")
    # psxSetNextBranchDelta: compare as signed 32-bit (next-cycle) > delay.
    diff = ((next_iop - cycle + 0x80000000) & 0xFFFFFFFF) - 0x80000000
    if diff > delay:
        if cycle + delay > 0xFFFFFFFFFFFFFFFF:
            raise Refusal("IOP next_event u64 addition would overflow")
        next_iop = cycle + delay
    updated = dict(scheduler)
    updated["interrupt"] = int(scheduler["interrupt"]) | (1 << IOP_EVENT_SIF0)
    updated["event_start"] = cycle
    updated["event_delta"] = delay
    updated["next_event"] = next_iop

    # PSX_INT's float multiplier is source-defined; PCSX2's PS2/PSX clock ratio
    # is exactly 8 for this configuration. Preserve float32 conversion explicitly.
    if "iop_cycle_ee" in scheduler:
        if "ee_cycle" not in scheduler or "ee_next_event" not in scheduler:
            raise Refusal("bridge update requires ee_cycle and ee_next_event")
        iop_cycle_ee = _native_int(scheduler, "iop_cycle_ee", -0x80000000, 0x7FFFFFFF)
        ee_cycle = _native_int(scheduler, "ee_cycle", 0, 0xFFFFFFFFFFFFFFFF)
        ee_next_event = _native_int(scheduler, "ee_next_event", 0, 0xFFFFFFFFFFFFFFFF)
        if ee_next_event < ee_cycle:
            raise Refusal("ee_next_event precedes ee_cycle; outside model domain")
        if "ee_dmastall" in scheduler:
            _native_int(scheduler, "ee_dmastall", 0, 0xFFFFFFFF)
        multiplier = ctypes.c_float(ctypes.c_float(1.0).value * 8.0).value
        iop_delta = int(ctypes.c_float((next_iop - cycle) * multiplier).value)
        if not 0 <= iop_delta <= 0x7FFFFFFF:
            raise Refusal("PSX_INT bridge float result outside s32 domain")
        updated["derived_iop_delta"] = iop_delta
        # R3000A.cpp invokes cpuSetNextEventDelta only inside this guard.
        if iop_cycle_ee < iop_delta:
            ee_delta = iop_delta - iop_cycle_ee
            if ee_delta > 0x7FFFFFFF:
                raise Refusal("EE bridge delta outside s32 domain")
            ee_diff = ((ee_next_event - ee_cycle + 0x80000000) & 0xFFFFFFFF) - 0x80000000
            if ee_diff > ee_delta:
                if ee_cycle + ee_delta > 0xFFFFFFFFFFFFFFFF:
                    raise Refusal("EE next_event u64 addition would overflow")
                updated["ee_next_event"] = ee_cycle + ee_delta
            updated["derived_ee_delta"] = ee_delta
    scheduler.clear()
    scheduler.update(updated)


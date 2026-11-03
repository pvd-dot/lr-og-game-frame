"""Synthetic source-contract tests for the bounded SIF0 due callback."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.emu.ps2_sif0 import complete_due_sif0
from tools.emu.ps2_sif0.due import (
    DMA2_IRQ,
    DMA_IRQ_MASTER,
    DMA_IRQ_MASTER_ENABLE,
    DMA9_START,
    IOP_DMA9_CHCR,
    IOP_DMA_ICR2,
    IOP_ICTRL,
    IOP_ISTAT,
    IOP_IRQ_DMA,
    IOP_WAIT_CYCLES,
)
from tools.emu.ps2_sif0.fifo import _put32, _u32
from tools.emu.ps2_sif0.state import HW_BYTES, IOP_EVENT_SIF0


def make_case(*, elapsed=20, deadline_delta=768, icr2=0x000D0400, ictrl=0):
    hw = bytearray(HW_BYTES)
    _put32(hw, IOP_DMA9_CHCR, 0x01000701)
    _put32(hw, IOP_DMA_ICR2, icr2)
    _put32(hw, IOP_ISTAT, 0)
    _put32(hw, IOP_ICTRL, ictrl)
    schedule = {
        "cycle": 10_000 + elapsed,
        "interrupt": 1 << IOP_EVENT_SIF0,
        "event_start": 10_000,
        "event_delta": 20,
        "next_event": 10_020,
        "counter_marker": "preserved",
    }
    # Keep the native counter origin/delta as separate inputs.  The modeled
    # source compares a signed delta against s32(cycle + wait - start).
    return hw, schedule, schedule["cycle"], deadline_delta


class DueEventTests(unittest.TestCase):
    def assert_atomic_refusal(self, hw, schedule, counter_start, counter_delta):
        before = bytes(hw), dict(schedule)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual((bytes(hw), dict(schedule)), before)

    def test_before_due_is_not_due_and_is_atomic(self):
        hw, schedule, counter_start, counter_delta = make_case(elapsed=19)
        before = bytes(hw), dict(schedule)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result["status"], "not_due", result)
        self.assertEqual((bytes(hw), dict(schedule)), before)

    def test_exact_due_clears_dma_sets_irq_and_reschedules_default(self):
        hw, schedule, counter_start, counter_delta = make_case()
        # ICTRL=0 prevents guest interrupt entry even if an interrupt is
        # already pending and unmasked; the DMA callback still raises I_STAT.
        _put32(hw, IOP_ISTAT, 0x20)
        _put32(hw, 0x1074, IOP_IRQ_DMA | 0x20)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result, {"status": "completed", "reason": ""})
        self.assertEqual(_u32(hw, IOP_DMA9_CHCR), 0x01000701 & ~DMA9_START)
        self.assertEqual(_u32(hw, IOP_DMA_ICR2), 0x000D0400 | DMA2_IRQ)
        self.assertEqual(_u32(hw, IOP_ISTAT), 0x20 | IOP_IRQ_DMA)
        self.assertEqual(_u32(hw, IOP_ICTRL), 0)
        self.assertEqual(schedule["interrupt"], 0)
        self.assertEqual(schedule["next_event"], schedule["cycle"] + IOP_WAIT_CYCLES)
        self.assertEqual(schedule["event_start"], 10_000)
        self.assertEqual(schedule["event_delta"], 20)
        self.assertEqual(schedule["counter_marker"], "preserved")

    def test_counter_deadline_before_default_horizon_is_selected(self):
        hw, schedule, counter_start, counter_delta = make_case(deadline_delta=137)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(schedule["next_event"], counter_start + counter_delta)

    def test_equal_counter_and_default_horizon_keeps_default_value(self):
        hw, schedule, counter_start, counter_delta = make_case(deadline_delta=IOP_WAIT_CYCLES)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(schedule["next_event"], schedule["cycle"] + IOP_WAIT_CYCLES)

    def test_dma_master_enable_sets_master_irq(self):
        hw, schedule, counter_start, counter_delta = make_case(icr2=0x00800400)
        result = complete_due_sif0(
            hw, schedule, counter_start=counter_start, counter_delta=counter_delta
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(
            _u32(hw, IOP_DMA_ICR2),
            0x00800400 | DMA2_IRQ | DMA_IRQ_MASTER,
        )

    def test_output_corruption_is_detected_by_full_mutable_projection(self):
        """The comparison catches a corrupted pending field in a completed output."""
        reference_hw, reference_schedule, counter_start, counter_delta = make_case()
        self.assertEqual(
            complete_due_sif0(
                reference_hw, reference_schedule,
                counter_start=counter_start, counter_delta=counter_delta,
            )["status"],
            "completed",
        )
        candidate_hw, candidate_schedule = bytearray(reference_hw), dict(reference_schedule)
        # Deliberately completed mutant: preserve all callback effects but
        # omit IopTestEvent's source-derived pending-bit clear.
        candidate_schedule["interrupt"] |= 1 << IOP_EVENT_SIF0
        self.assertNotEqual((bytes(candidate_hw), candidate_schedule),
                            (bytes(reference_hw), reference_schedule))
        self.assertNotEqual(candidate_schedule["interrupt"], reference_schedule["interrupt"])

    def test_additional_pending_event_refuses_atomically(self):
        hw, schedule, counter_start, counter_delta = make_case()
        schedule["interrupt"] |= 1 << 3
        self.assert_atomic_refusal(hw, schedule, counter_start, counter_delta)

    def test_counter_already_due_refuses_atomically(self):
        hw, schedule, counter_start, _ = make_case()
        self.assert_atomic_refusal(hw, schedule, counter_start, 0)

    def test_due_callback_requires_scheduler_to_point_at_sole_due_event(self):
        hw, schedule, counter_start, counter_delta = make_case()
        schedule["next_event"] += 1
        self.assert_atomic_refusal(hw, schedule, counter_start, counter_delta)

    def test_nonzero_ictrl_refuses_irq_scheduling_domain_atomically(self):
        hw, schedule, counter_start, counter_delta = make_case(ictrl=1)
        self.assert_atomic_refusal(hw, schedule, counter_start, counter_delta)

    def test_invalid_time_width_and_unreachable_order_refuse_atomically(self):
        hw, schedule, counter_start, counter_delta = make_case()
        schedule["event_delta"] = 1 << 31
        self.assert_atomic_refusal(hw, schedule, counter_start, counter_delta)
        hw, schedule, counter_start, counter_delta = make_case()
        schedule["cycle"] = schedule["event_start"] - 1
        self.assert_atomic_refusal(hw, schedule, counter_start, counter_delta)

    def test_counter_horizon_signed_wrap_refuses_atomically(self):
        hw, schedule, _, _ = make_case()
        # The counter itself is not due, but R3000A narrows
        # cycle + wait - counter_start to s32 before comparing. This valid
        # u64 fixture crosses that signed horizon without wrapping the
        # counter's elapsed-cycle check first.
        schedule["cycle"] = 10_000_000_000
        schedule["event_start"] = schedule["cycle"] - 20
        schedule["next_event"] = schedule["cycle"]
        start = schedule["cycle"] - (((1 << 31) - 1) - 100)
        before = bytes(hw), dict(schedule)
        result = complete_due_sif0(
            hw, schedule, counter_start=start, counter_delta=(1 << 31) - 1
        )
        self.assertEqual(result["status"], "refused", result)
        self.assertIn("counter horizon narrowing", result["reason"])
        self.assertEqual((bytes(hw), dict(schedule)), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)

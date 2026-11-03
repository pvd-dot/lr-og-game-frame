"""Synthetic source-contract tests for IOP exception-entry effects."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.emu.ps2_iop_irq import (  # noqa: E402
    BEV_EXCEPTION_VECTOR,
    HW_BYTES,
    IOP_ICTRL,
    IOP_IMASK,
    IOP_ISTAT,
    NORMAL_EXCEPTION_VECTOR,
    enter_iop_interrupt,
)


def put32(buf, offset, value):
    buf[offset : offset + 4] = value.to_bytes(4, "little")


def make_hw(*, ictrl=1, istat=0x28, imask=0x20):
    hw = bytearray(HW_BYTES)
    put32(hw, IOP_ICTRL, ictrl)
    put32(hw, IOP_ISTAT, istat)
    put32(hw, IOP_IMASK, imask)
    return hw


class IopInterruptEntryTests(unittest.TestCase):
    def test_taken_normal_vector_clears_exception_code_and_pushes_status(self):
        hw = make_hw()
        before = bytes(hw)
        result = enter_iop_interrupt(
            hw,
            pc=0x00004EA0,
            status=0x00008415,
            cause=0xA5A5126F,
            epc=0x12345678,
        )
        self.assertEqual(result, {
            "disposition": "taken",
            "reason": "",
            "pc": NORMAL_EXCEPTION_VECTOR,
            "status": 0x00008414,
            "cause": 0xA5A51200,
            "epc": 0x00004EA0,
        })
        self.assertEqual(bytes(hw), before)

    def test_taken_bev_vector_preserves_cause_bd_and_high_status(self):
        hw = make_hw(istat=1, imask=1)
        result = enter_iop_interrupt(
            hw,
            pc=0x80001234,
            status=0x00400401,
            cause=0x8000017F,
            epc=0xDEADBEEF,
        )
        self.assertEqual(result["disposition"], "taken")
        self.assertEqual(result["pc"], BEV_EXCEPTION_VECTOR)
        self.assertEqual(result["epc"], 0x80001234)
        self.assertEqual(result["cause"], 0x80000100)
        self.assertEqual(result["status"], 0x00400404)

    def test_exact_status_gate_threshold_is_inclusive(self):
        hw = make_hw()
        result = enter_iop_interrupt(hw, pc=0x1000, status=0x401, cause=0x55, epc=0x99)
        self.assertEqual(result["disposition"], "taken")

    def test_each_false_gate_returns_cp0_unchanged(self):
        cases = (
            (make_hw(ictrl=0), 0x401),
            (make_hw(istat=0x08, imask=0x20), 0x401),
            (make_hw(), 0x400),
        )
        for hw, status in cases:
            before = bytes(hw)
            result = enter_iop_interrupt(
                hw, pc=0x2000, status=status, cause=0x87654321, epc=0xABCDEF00
            )
            self.assertEqual(result, {
                "disposition": "not_taken",
                "reason": "IOP interrupt-entry predicate is false",
                "pc": 0x2000,
                "status": status,
                "cause": 0x87654321,
                "epc": 0xABCDEF00,
            })
            self.assertEqual(bytes(hw), before)

    def test_invalid_inputs_refuse_without_hardware_mutation(self):
        valid = make_hw()
        before = bytes(valid)
        for hw, values in (
            (bytearray(HW_BYTES - 1), (0x1000, 0x401, 0, 0)),
            (memoryview(valid), (0x1000, 0x401, 0, 0)),
            (valid, (0x1001, 0x401, 0, 0)),
            (valid, (True, 0x401, 0, 0)),
            (valid, (0x1000, -1, 0, 0)),
            (valid, (0x1000, 0x401, 1 << 32, 0)),
        ):
            with self.subTest(values=values, hw_type=type(hw).__name__, hw_len=len(hw)):
                result = enter_iop_interrupt(
                    hw, pc=values[0], status=values[1], cause=values[2], epc=values[3]
                )
                self.assertEqual(result["disposition"], "refused")
        self.assertEqual(bytes(valid), before)


if __name__ == "__main__":
    unittest.main()

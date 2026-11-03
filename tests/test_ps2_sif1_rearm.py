#!/usr/bin/env python3
"""Synthetic contract tests for the bounded PS2 SIF1 rearm transition."""

import ctypes
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HW_BYTES = 0x10000
SIF_BYTES = 0x244


class PrivateState(ctypes.Structure):
    _fields_ = [("dma_stall", ctypes.c_uint8), ("reserved", ctypes.c_uint8 * 3)]


class Sif1RearmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="gf-sif1-rearm-")
        library = pathlib.Path(cls.tmp.name) / "libsif1_rearm.so"
        subprocess.run(
            [
                os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-shared", "-fPIC", str(ROOT / "tools/emu/ps2_sif1_rearm.c"), "-o", str(library),
            ],
            check=True,
        )
        cls.lib = ctypes.CDLL(str(library))
        cls.fn = cls.lib.gf_ps2_sif1_empty_fifo_rearm
        p8 = ctypes.POINTER(ctypes.c_uint8)
        cls.fn.argtypes = [p8, ctypes.c_size_t, p8, ctypes.c_size_t, p8,
                           ctypes.c_size_t, ctypes.POINTER(PrivateState),
                           ctypes.POINTER(PrivateState)]
        cls.fn.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def put32(buf, offset, value):
        buf[offset:offset + 4] = int(value).to_bytes(4, "little")

    @staticmethod
    def get32(buf, offset):
        return int.from_bytes(bytes(buf[offset:offset + 4]), "little")

    def make_case(self, *, stall=0, madr=0x1000, stadr=0x2000):
        iop_hw = bytearray(HW_BYTES)
        ee_hw = bytearray(HW_BYTES)
        sif = bytearray((i * 17 + 3) & 0xff for i in range(SIF_BYTES))

        # Guest-side DMA10 setup has completed before this API boundary.
        self.put32(iop_hw, 0x1534, 0x00200020)
        self.put32(iop_hw, 0x1538, 0x41000300)
        self.put32(iop_hw, 0x1570, 0x00008000)
        # Empty FIFO and idle EE SIF1 channel; this path does not read cursors.
        self.put32(sif, 0x210, 0xffffffff)
        self.put32(sif, 0x214, 0x80000000)
        self.put32(sif, 0x218, 0)
        sif[0x21c] = 0
        sif[0x21d] = 0
        self.put32(sif, 0x220, 0x12345678)
        sif[0x224] = 1
        sif[0x225] = 0
        self.put32(sif, 0x228, 0x23456789)
        self.put32(ee_hw, 0xc410, madr)
        self.put32(ee_hw, 0xc420, 0)
        self.put32(ee_hw, 0xe060, stadr)
        self.put32(ee_hw, 0xf240, 0xdeadffff)
        return iop_hw, ee_hw, sif, PrivateState(stall, (0, 0, 0))

    def invoke(self, iop_hw, ee_hw, sif, pin, pout=None,
               *, iop_size=HW_BYTES, ee_size=HW_BYTES, sif_size=SIF_BYTES):
        if pout is None:
            pout = PrivateState(0xaa, (0xaa, 0xaa, 0xaa))
        iop_view = (ctypes.c_uint8 * len(iop_hw)).from_buffer(iop_hw)
        ee_view = (ctypes.c_uint8 * len(ee_hw)).from_buffer(ee_hw)
        sif_view = (ctypes.c_uint8 * len(sif)).from_buffer(sif)
        status = self.fn(iop_view, iop_size, ee_view, ee_size, sif_view, sif_size,
                         ctypes.byref(pin), ctypes.byref(pout))
        return status, pout

    def test_empty_idle_path_resets_cycles_and_clears_only_sbus_bits(self):
        iop_hw, ee_hw, sif, pin = self.make_case()
        untouched_iop = bytes(iop_hw)
        status, pout = self.invoke(iop_hw, ee_hw, sif, pin)
        self.assertEqual(status, 1)
        self.assertEqual((sif[0x225], sif[0x224]), (1, 0))
        self.assertEqual((self.get32(sif, 0x220), self.get32(sif, 0x228)), (0, 0))
        self.assertEqual(self.get32(ee_hw, 0xf240), 0xdeadbfbf)
        self.assertEqual(pout.dma_stall, 0)
        self.assertEqual(bytes(pout.reserved), b"\0\0\0")
        self.assertEqual(bytes(iop_hw), untouched_iop)
        self.assertEqual((self.get32(sif, 0x210), self.get32(sif, 0x214),
                          self.get32(sif, 0x218)), (0xffffffff, 0x80000000, 0))

    def test_ee_end_flag_is_not_consulted_when_ee_channel_is_idle(self):
        iop_hw, ee_hw, sif, pin = self.make_case()
        sif[0x21c] = 1
        status, _ = self.invoke(iop_hw, ee_hw, sif, pin)
        self.assertEqual(status, 1)
        self.assertEqual(sif[0x21c], 1)
        self.assertEqual(sif[0x225], 1)

    def test_private_stall_past_stadr_takes_early_return_after_iop_busy_writes(self):
        iop_hw, ee_hw, sif, pin = self.make_case(stall=1, madr=0x3000, stadr=0x2000)
        before_sbus = self.get32(ee_hw, 0xf240)
        before_cycles = (self.get32(sif, 0x220), self.get32(sif, 0x228))
        status, pout = self.invoke(iop_hw, ee_hw, sif, pin)
        self.assertEqual(status, 1)
        self.assertEqual((sif[0x225], sif[0x224]), (1, 0))
        self.assertEqual((self.get32(sif, 0x220), self.get32(sif, 0x228)), before_cycles)
        self.assertEqual(self.get32(ee_hw, 0xf240), before_sbus)
        self.assertEqual(pout.dma_stall, 1)

    def test_private_stall_with_madr_at_or_below_stadr_runs_rearm(self):
        iop_hw, ee_hw, sif, pin = self.make_case(stall=1, madr=0x2000, stadr=0x2000)
        status, pout = self.invoke(iop_hw, ee_hw, sif, pin)
        self.assertEqual(status, 1)
        self.assertEqual((self.get32(sif, 0x220), self.get32(sif, 0x228)), (0, 0))
        self.assertEqual(self.get32(ee_hw, 0xf240), 0xdeadbfbf)
        self.assertEqual(pout.dma_stall, 0)

    def test_private_state_supports_exact_in_place_update(self):
        iop_hw, ee_hw, sif, pin = self.make_case(stall=1)
        iop_view = (ctypes.c_uint8 * len(iop_hw)).from_buffer(iop_hw)
        ee_view = (ctypes.c_uint8 * len(ee_hw)).from_buffer(ee_hw)
        sif_view = (ctypes.c_uint8 * len(sif)).from_buffer(sif)
        status = self.fn(iop_view, HW_BYTES, ee_view, HW_BYTES, sif_view,
                         SIF_BYTES, ctypes.byref(pin), ctypes.byref(pin))
        self.assertEqual(status, 1)
        self.assertEqual(pin.dma_stall, 0)
        self.assertEqual(bytes(pin.reserved), b"\0\0\0")
        self.assertEqual(self.get32(sif, 0x220), 0)
        self.assertEqual(self.get32(sif, 0x228), 0)

    def assert_refused_atomically(self, *, edit=None, bad_iop_size=HW_BYTES,
                                  bad_ee_size=HW_BYTES, bad_sif_size=SIF_BYTES):
        iop_hw, ee_hw, sif, pin = self.make_case()
        if edit:
            edit(iop_hw, ee_hw, sif, pin)
        before = (bytes(iop_hw), bytes(ee_hw), bytes(sif))
        pout = PrivateState(0x55, (0x66, 0x77, 0x88))
        before_out = bytes(pout)
        iop_view = (ctypes.c_uint8 * len(iop_hw)).from_buffer(iop_hw)
        ee_view = (ctypes.c_uint8 * len(ee_hw)).from_buffer(ee_hw)
        sif_view = (ctypes.c_uint8 * len(sif)).from_buffer(sif)
        status = self.fn(iop_view, bad_iop_size, ee_view, bad_ee_size, sif_view,
                         bad_sif_size, ctypes.byref(pin), ctypes.byref(pout))
        self.assertEqual(status, 0)
        self.assertEqual((bytes(iop_hw), bytes(ee_hw), bytes(sif)), before)
        self.assertEqual(bytes(pout), before_out)

    def test_refuses_nonempty_fifo_and_busy_ee_without_mutation(self):
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: self.put32(sif, 0x218, 1))
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: sif.__setitem__(0x21d, 1))

    def test_refuses_wrong_dma_configuration_and_nonzero_qwc_atomically(self):
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: self.put32(ih, 0x1538, 0x41000200))
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: self.put32(ih, 0x1570, 0))
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: self.put32(eh, 0xc420, 1))

    def test_refuses_invalid_private_state_lengths_and_host_alias(self):
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: setattr(p, "dma_stall", 2))
        self.assert_refused_atomically(edit=lambda ih, eh, sif, p: p.reserved.__setitem__(0, 1))
        self.assert_refused_atomically(bad_iop_size=HW_BYTES - 1)
        self.assert_refused_atomically(bad_ee_size=HW_BYTES - 1)
        self.assert_refused_atomically(bad_sif_size=SIF_BYTES - 1)

        iop_hw, ee_hw, sif, pin = self.make_case()
        before = bytes(iop_hw)
        iop_view = (ctypes.c_uint8 * len(iop_hw)).from_buffer(iop_hw)
        ee_view = (ctypes.c_uint8 * len(ee_hw)).from_buffer(ee_hw)
        sif_view = (ctypes.c_uint8 * len(sif)).from_buffer(sif)
        status = self.fn(iop_view, HW_BYTES, ee_view, HW_BYTES, sif_view, SIF_BYTES,
                         ctypes.byref(pin), ctypes.cast(iop_view, ctypes.POINTER(PrivateState)))
        self.assertEqual(status, 0)
        self.assertEqual(bytes(iop_hw), before)


if __name__ == "__main__":
    unittest.main()

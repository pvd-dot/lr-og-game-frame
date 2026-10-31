"""Hand-authored synthetic regressions for source-derived partial SIF0 paths."""
import struct
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import importlib
m = importlib.import_module("tools.emu.ps2_sif0.launch")
from tools.emu.ps2_sif0 import launch as public_launch


class GeneratedJunkTests(unittest.TestCase):
    def make_fifo(self, write_pos: int, words: int) -> bytearray:
        sif = bytearray(m.SIF_BYTES)
        m._puts32(sif, m.FIFO_WRITE, write_pos)
        for i in range(m.FIFO_WORDS):
            m._put32(sif, m.FIFO_DATA + i * 4, 0xA0000000 + i)
        return sif

    def test_nonwrapping_partial_write_serializes_source_scratch(self):
        sif = self.make_fifo(20, 2)
        # Sif.h: transferredWords=2, prevQW=(20-(4+2))=14.
        m._write_junk(sif, 2)
        self.assertEqual(struct.unpack_from("<4I", sif, m.FIFO_JUNK),
                         tuple(0xA0000000 + i for i in range(14, 18)))
        self.assertEqual(m._u32(sif, m.FIFO_DATA + 20 * 4), 0xA0000010)
        self.assertEqual(m._u32(sif, m.FIFO_DATA + 21 * 4), 0xA0000011)
        self.assertEqual((m._s32(sif, m.FIFO_WRITE), m._s32(sif, m.FIFO_SIZE)), (22, 2))

    def test_wrapped_second_chunk_uses_global_source_index(self):
        sif = self.make_fifo(127, 2)
        # Sif.h: prevQW=(127-(4+2))&127=121; wP0=1, wP1=1.
        m._write_junk(sif, 2)
        self.assertEqual(struct.unpack_from("<4I", sif, m.FIFO_JUNK),
                         tuple(0xA0000000 + i for i in range(121, 125)))
        self.assertEqual(m._u32(sif, m.FIFO_DATA + 127 * 4), 0xA000007C)
        self.assertEqual(m._u32(sif, m.FIFO_DATA), 0xA000007A)
        self.assertEqual((m._s32(sif, m.FIFO_WRITE), m._s32(sif, m.FIFO_SIZE)), (1, 2))


class DomainAndAtomicityTests(unittest.TestCase):
    def test_public_api_and_help(self):
        self.assertIs(public_launch, m.launch)
        result = subprocess.run(
            [sys.executable, "-m", "tools.emu.ps2_sif0", "--help"],
            cwd=ROOT, text=True, capture_output=True, check=True
        )
        self.assertIn("launch()", result.stdout)

    def inputs(self):
        return (bytearray(m.IOP_RAM_BYTES), bytearray(m.HW_BYTES),
                bytearray(m.EE_RAM_BYTES), bytearray(m.HW_BYTES),
                bytearray(m.SIF_BYTES),
                {"cycle": 7, "interrupt": 0, "event_start": 0,
                 "event_delta": 0, "next_event": 99})

    def test_exact_array_sizes_refuse_without_mutation(self):
        vals = self.inputs()
        iop, ih, ee, eh, sf, sch = vals
        before = tuple(bytes(x) for x in vals[:5]), dict(sch)
        result = m.launch(iop[:-1], ih, ee, eh, sf, sch)
        self.assertEqual(result["status"], "refused")
        self.assertEqual((tuple(bytes(x) for x in vals[:5]), dict(sch)), before)

    def test_scheduler_native_width_and_order_refuse(self):
        # Scheduler boundary is validated by the source-shaped scheduler helper.
        with self.assertRaises(m.Refusal):
            m._schedule_iop({"cycle": 1 << 64, "interrupt": 0, "event_start": 0,
                             "event_delta": 0, "next_event": (1 << 64) - 1}, 1)
        with self.assertRaises(m.Refusal):
            m._schedule_iop({"cycle": 100, "interrupt": 0,
                             "event_start": 0, "event_delta": 0,
                             "next_event": 99}, 1)

    def test_scheduler_overflow_refuses_live_launch_atomically(self):
        iop, ih, ee, eh, sf, sch = self.inputs()
        m._put32(ih, m.IOP_DMA9_TADR, 0x100)
        m._put32(ih, m.IOP_DMA_PCR2, 0x800)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_CHCR, 0x10000184)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_MADR, 0x123400)
        sf[m.EE_BUSY] = 1
        struct.pack_into("<IIII", iop, 0x100, 0x80000200, 4, 0x10000001, 0x123400)
        struct.pack_into("<IIII", iop, 0x110, 0x80000210, 4, 0x10000001, 0x123410)
        struct.pack_into("<4I", iop, 0x200, 1, 2, 3, 4)
        struct.pack_into("<4I", iop, 0x210, 5, 6, 7, 8)
        sch["cycle"] = 1 << 64
        before = tuple(bytes(x) for x in (iop, ih, ee, eh, sf)), dict(sch)
        result = m.launch(iop, ih, ee, eh, sf, sch)
        self.assertEqual(result["status"], "refused")
        self.assertEqual((tuple(bytes(x) for x in (iop, ih, ee, eh, sf)), dict(sch)), before)

    def test_aliased_memories_and_non_u32_chcr_refuse(self):
        iop, ih, ee, eh, sf, sch = self.inputs()
        before = tuple(bytes(x) for x in (iop, ih, ee, sf)), dict(sch)
        aliased = m.launch(iop, ih, ee, ih, sf, sch)
        self.assertEqual(aliased["status"], "refused")
        self.assertIn("distinct", aliased["reason"])
        self.assertEqual((tuple(bytes(x) for x in (iop, ih, ee, sf)), dict(sch)), before)
        invalid_chcr = m.launch(iop, ih, ee, eh, sf, sch, True)
        self.assertEqual(invalid_chcr["status"], "refused")
        self.assertEqual(m.launch(iop, ih, ee, eh, sf, sch, 0)["status"], "refused")
        self.assertEqual(m.launch(iop, ih, ee, eh, sf, sch)["status"], "refused")  # PCR disabled

        malformed_flags = bytearray(sf)
        malformed_flags[m.EE_BUSY] = 2
        m._put32(ih, m.IOP_DMA_PCR2, 0x800)
        self.assertEqual(m.launch(iop, ih, ee, eh, malformed_flags, sch)["status"], "refused")

    def test_dmac_stall_register_constants_and_sts_decode(self):
        # CTRL.STS is bits 4..5 and STADR is the EE MMIO word at 0xE060.
        eh = bytearray(m.HW_BYTES)
        m._put32(eh, m.EE_DMAC_CTRL, 1 << 4)
        self.assertEqual((m._u32(eh, m.EE_DMAC_CTRL) >> 4) & 3, 1)
        m._put32(eh, m.EE_DMAC_STADR, 0x123450)
        self.assertEqual(m._u32(eh, 0xE060), 0x123450)

    def test_live_sts_sif0_cnts_updates_stadr(self):
        iop, ih, ee, eh, sf, sch = self.inputs()
        m._put32(ih, m.IOP_DMA9_TADR, 0x100)
        m._put32(ih, m.IOP_DMA_PCR2, 0x800)
        m._put32(eh, m.EE_DMAC_CTRL, 1 << 4)  # CTRL.STS=SIF0
        m._put32(eh, m.EE_DMAC_STADR, 0x7770)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_CHCR, 0x10000184)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_MADR, 0x123400)
        sf[m.EE_BUSY] = 1
        struct.pack_into("<IIII", iop, 0x100, 0x300, 4, 1, 0x123400)
        struct.pack_into("<IIII", iop, 0x110, 0x80000310, 4, 1, 0x123410)
        struct.pack_into("<4I", iop, 0x300, 1, 2, 3, 4)
        struct.pack_into("<4I", iop, 0x310, 5, 6, 7, 8)
        result = m.launch(iop, ih, ee, eh, sf, sch)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(m._u32(eh, m.EE_DMAC_STADR), 0x123420)
        self.assertEqual(ee[0x123400:0x123420], struct.pack("<8I", 1, 2, 3, 4, 5, 6, 7, 8))

    def test_zero_word_iop_end_serializes_minimum_event_cycles(self):
        iop, ih, ee, eh, sf, sch = self.inputs()
        m._put32(ih, m.IOP_DMA9_TADR, 0x100)
        m._put32(ih, m.IOP_DMA_PCR2, 0x800)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_CHCR, 0x10000184)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_MADR, 0x400)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_QWC, 1)
        sf[m.EE_BUSY] = 1
        # An already pending EE payload consumes these four tag words. The
        # zero-word IOP end still serializes EndIOP's minimum cycle count.
        struct.pack_into("<6I", iop, 0x100, 0x80000300, 0,
                         0x10000001, 0x400, 0x12, 0x34)
        result = m.launch(iop, ih, ee, eh, sf, sch)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(ee[0x400:0x410], struct.pack("<4I", 0x10000001, 0x400, 0x12, 0x34))
        self.assertEqual(m._s32(sf, m.IOP_CYCLES), 1)
        self.assertEqual(sch["event_delta"], 1)

    def test_generated_partial_launch_wraps_junk_and_serializes_scratch(self):
        iop, ih, ee, eh, sf, sch = self.inputs()
        m._put32(ih, m.IOP_DMA9_TADR, 0x100)
        m._put32(ih, m.IOP_DMA_PCR2, 0x800)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_CHCR, 0x10000184)
        m._put32(eh, m.EE_SIF0_CH + m.EE_DMACH_MADR, 0x123400)
        sf[m.EE_BUSY] = 1
        # The four-word tag starts at FIFO position 122, then one payload word
        # leaves writePos=127 when generated writeJunk(3) runs.
        m._puts32(sf, m.FIFO_READ, 122)
        m._puts32(sf, m.FIFO_WRITE, 122)
        struct.pack_into("<IIII", iop, 0x100, 0x80000300, 1,
                         0x10000001, 0x123400)
        struct.pack_into("<II", iop, 0x110, 0xDEADBEEF, 0xCAFEBABE)
        struct.pack_into("<I", iop, 0x300, 0x55667788)
        result = m.launch(iop, ih, ee, eh, sf, sch)
        self.assertEqual(result["status"], "completed", result)
        # The EE receives payload followed by source junk[1..3]. The second
        # source chunk is wrapped and must use global source indices 1 and 2.
        self.assertEqual(ee[0x123400:0x123410],
                         struct.pack("<4I", 0x55667788, 0xCAFEBABE, 0x123400, 0xDEADBEEF))
        self.assertEqual(struct.unpack_from("<4I", sf, m.FIFO_JUNK),
                         (0x10000001, 0x123400, 0xDEADBEEF, 0xCAFEBABE))


if __name__ == "__main__":
    unittest.main(verbosity=2)

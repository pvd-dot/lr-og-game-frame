"""Synthetic MIPS programs authored for these tests; no commercial code required."""
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'emu'))
from ps2_emu import Emu, Unsupported, allowed


def elf(words):
    code = struct.pack('<' + 'I' * len(words), *words)
    header = struct.pack('<16sHHIIIIIHHHHHH', b'\x7fELF\x01\x01\x01' + bytes(9),
                         2, 8, 1, 0x100000, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    ph = struct.pack('<8I', 1, 84, 0x100000, 0x100000, len(code), len(code), 5, 4)
    return header + ph + code


def daddu(rd, rs, rt, shamt=0):
    return (rs << 21) | (rt << 16) | (rd << 11) | (shamt << 6) | 0x2d


def daddiu(rt, rs, imm):
    return (25 << 26) | (rs << 21) | (rt << 16) | (imm & 0xffff)


def ld(rt, rs, offset):
    return (55 << 26) | (rs << 21) | (rt << 16) | (offset & 0xffff)


def sd(rt, rs, offset):
    return (63 << 26) | (rs << 21) | (rt << 16) | (offset & 0xffff)


class Tests(unittest.TestCase):
    def engine(self, words, mode='mips32'):
        data = elf(words)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'sample.elf'
        path.write_bytes(data)
        return Emu(path, hashlib.sha256(data).hexdigest(), mode=mode)

    def test_integer_memory_and_return_delay_slot(self):
        # Our own sample: add A0+A1, store to A2, return result+1 in delay slot.
        emu = self.engine([0x00851021, 0xacc20000, 0x03e00008, 0x24420001])
        self.assertEqual(emu.call(0x100000, 0x100010, 8, 13, 0x200000), 22)
        self.assertEqual(emu.read(0x200000, 4), struct.pack('<I', 21))
        self.assertEqual(emu.written, set(range(0x200000, 0x200004)))
        self.assertEqual(emu.steps, 4)

    def test_word_overflow(self):
        emu = self.engine([0x00851021, 0x03e00008, 0])
        self.assertEqual(emu.call(0x100000, 0x10000c, 0xffffffff, 2), 1)

    def test_daddu_zero_operand_copies_low_word(self):
        # DADDU V0,A0,ZERO followed by JR RA; the input register is 32-bit here.
        emu = self.engine([daddu(2, 4, 0), 0x03e00008, 0])
        self.assertEqual(emu.call(0x100000, 0x10000c, 0xfedcba98), 0xfedcba98)

    def test_daddu_destination_aliases_source_and_repeated_calls(self):
        # DADDU A0,A0,ZERO; ADDU V0,A0,ZERO; JR RA.
        emu = self.engine([daddu(4, 4, 0), 0x00801021, 0x03e00008, 0])
        for value in (0, 1, 0xffffffff, 0x87654321):
            self.assertEqual(emu.call(0x100000, 0x100010, value), value)
            self.assertEqual(emu.steps, 4)  # includes JR RA's executed delay slot

    def test_daddu_zero_register_and_zero_source_form(self):
        # A write to ZERO is discarded; ZERO as either source remains zero.
        emu = self.engine([daddu(0, 4, 0), daddu(2, 0, 0), 0x03e00008, 0])
        self.assertEqual(emu.call(0x100000, 0x100010, 0xffffffff), 0)

    def test_daddu_zero_rs_copies_rt_and_allows_destination_alias(self):
        # DADDU A1,ZERO,A1; ADDU V0,A1,ZERO; JR RA.
        emu = self.engine([daddu(5, 0, 5), 0x00a01021, 0x03e00008, 0])
        self.assertEqual(emu.call(0x100000, 0x100010, 0, 0xdeadbeef), 0xdeadbeef)

    def test_daddu_rejects_arithmetic_and_reserved_encoding(self):
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            self.engine([daddu(2, 4, 5), 0x03e00008, 0]).call(0x100000, 0x10000c, 1, 2)
        self.assertFalse(allowed(daddu(2, 4, 0, shamt=1)))

    def test_daddu_in_delay_slot_fails_closed(self):
        # JR RA's delay slot is part of execution and must not be skipped.
        emu = self.engine([0x03e00008, daddu(2, 4, 0), 0])
        with self.assertRaisesRegex(Unsupported, 'delay slot'):
            emu.call(0x100000, 0x10000c, 7)

    def test_daddu_branch_delay_rejected_when_taken_or_untaken(self):
        # BEQ's delay instruction executes on both paths; reject before branching.
        emu = self.engine([0x10850002, daddu(2, 4, 0), 0x24420001,
                           0x03e00008, 0])
        for left, right in ((4, 4), (4, 5)):
            with self.assertRaisesRegex(Unsupported, 'delay slot'):
                emu.call(0x100000, 0x100014, left, right)

    def test_mips64_daddu_copy_runs_in_branch_delay_and_preserves_memory_order(self):
        # The copy executes in BEQ's delay slot on both paths. The branch chooses
        # whether the first store runs; both stores observe the full-width copy.
        words = [0x10850002,             # beq a0,a1,+2
                 daddu(8, 4, 0),          # delay: T0 = A0
                 0xacc80000,              # sw T0,0(A2), not-taken path only
                 0xacc80004,              # sw T0,4(A2)
                 daddu(2, 8, 0),          # V0 = T0
                 0x03e00008, 0]           # return; nop
        emu = self.engine(words, mode='mips64')
        value = 0x8123456789abcdef

        # Taken branch skips only the first store; the delay-slot copy still runs.
        emu.write(0x200000, bytes(8))
        self.assertEqual(emu.call(0x100000, 0x10001c, value, value, 0x200000), value)
        self.assertEqual(emu.read(0x200000, 8), bytes(4) + struct.pack('<I', value & 0xffffffff))
        self.assertEqual(emu.steps, 6)

        # Not-taken branch executes both stores after the same delay-slot copy.
        emu.write(0x200000, bytes(8))
        self.assertEqual(emu.call(0x100000, 0x10001c, value, value ^ 1, 0x200000), value)
        self.assertEqual(emu.read(0x200000, 8),
                         struct.pack('<II', value & 0xffffffff, value & 0xffffffff))
        self.assertEqual(emu.steps, 7)

    def test_mips64_daddu_native_copy_aliases_and_arithmetic_rejection(self):
        for word, result_reg, expected in ((daddu(4, 4, 0), 4, 0x8123456789abcdef),
                                           (daddu(5, 0, 5), 5, 0xfedcba9876543210)):
            emu = self.engine([word, daddu(2, result_reg, 0),
                               0x03e00008, 0], mode='mips64')
            values = [0x8123456789abcdef, 0xfedcba9876543210]
            self.assertEqual(emu.call(0x100000, 0x100010, *values), expected)

        # A write to $zero is ignored by the native MIPS64 execution path.
        emu = self.engine([daddu(0, 4, 0), 0x03e00008, 0], mode='mips64')
        self.assertEqual(emu.call(0x100000, 0x10000c, 0x8123456789abcdef), 0)

        emu = self.engine([daddu(2, 4, 5), 0x03e00008, 0], mode='mips64')
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            emu.call(0x100000, 0x10000c, 0x8123456789abcdef, 7)

    def test_daddu_cannot_run_past_function_end(self):
        emu = self.engine([daddu(2, 4, 0), 0x03e00008, 0])
        with self.assertRaisesRegex(Unsupported, 'escaped'):
            emu.call(0x100000, 0x100004, 7)

    @staticmethod
    def dshift(rt, rd, sa, fn, rs=0):
        return (rs << 21) | (rt << 16) | (rd << 11) | (sa << 6) | fn

    def test_mips64_dsll32_and_dsrl32_match_64bit_arithmetic(self):
        # DSLL32 A0,T0,sa; DADDU V0,T0,ZERO; JR RA; delay slot.
        for sa in (0, 1, 15, 30, 31):
            emu = self.engine([self.dshift(4, 8, sa, 0x3c), daddu(2, 8, 0),
                               0x03e00008, 0], mode='mips64')
            for value in (0, 1, 0xffffffff, 0x0123456789abcdef,
                          0x8000000000000000, 0xffffffffffffffff):
                expected = (value << (32 + sa)) & 0xffffffffffffffff
                self.assertEqual(emu.call(0x100000, 0x100010, value), expected)

        # DSRL32 includes source bits above bit 31; these cannot be represented
        # by the existing MIPS32 register mode.
        for sa in (0, 1, 15, 30, 31):
            emu = self.engine([self.dshift(4, 8, sa, 0x3e), daddu(2, 8, 0),
                               0x03e00008, 0], mode='mips64')
            for value in (0, 0xffffffff, 0x0123456789abcdef,
                          0x8000000000000000, 0xffffffffffffffff):
                expected = value >> (32 + sa)
                self.assertEqual(emu.call(0x100000, 0x100010, value), expected)

    def test_mips64_shift_register_alias_and_r5900_flag_extraction(self):
        # The original's byte-bit test is DSLL32 by 30 followed by DSRL32 by 31.
        emu = self.engine([self.dshift(4, 4, 30, 0x3c),
                           self.dshift(4, 2, 31, 0x3e),
                           0x03e00008, 0], mode='mips64')
        for value in (0, 1, 2, 3, 0x80, 0x82, 0xff):
            self.assertEqual(emu.call(0x100000, 0x100010, value), (value >> 1) & 1)

    def test_mips64_mode_keeps_common_memory_branch_and_delay_behavior(self):
        # LW sign-extends in 64-bit mode, ADDIU wraps/sign-extends as MIPS64,
        # SW stores the low word, and BEQ executes its delay slot either way.
        words = [0x8c820000,             # lw v0,0(a0)
                 0x2442ffff,             # addiu v0,v0,-1
                 0xac820004,             # sw v0,4(a0)
                 0x10850002,             # beq a0,a1, +2
                 0x24420001,             # delay: addiu v0,v0,1
                 0x24420002,             # not-taken path
                 0x03e00008, 0]
        emu = self.engine(words, mode='mips64')
        emu.write(0x200000, struct.pack('<I', 0x80000000) + bytes(4))
        result = emu.call(0x100000, 0x100020, 0x200000, 0x200000)
        self.assertEqual(result, 0xffffffff80000000)
        self.assertEqual(emu.read(0x200004, 4), struct.pack('<I', 0x7fffffff))
        self.assertEqual(emu.steps, 7)

        emu32 = self.engine(words)
        emu32.write(0x200000, struct.pack('<I', 0x80000000) + bytes(4))
        result32 = emu32.call(0x100000, 0x100020, 0x200000, 0x200000)
        self.assertEqual(result32, result & 0xffffffff)
        self.assertEqual(emu32.read(0x200004, 4), emu.read(0x200004, 4))

        # A 32-bit negative input must be sign-extended by the caller in this
        # opt-in 64-bit ABI view for signed 64-bit branches to retain its meaning.
        signed_branch = self.engine([0x1c800002, 0x24020001, 0x24020007,
                                    0x03e00008, 0], mode='mips64')  # bgtz a0,+2
        result64 = signed_branch.call(0x100000, 0x100014, 0xffffffffffffffff)
        branch32 = self.engine([0x1c800002, 0x24020001, 0x24020007,
                                0x03e00008, 0])
        result32 = branch32.call(0x100000, 0x100014, 0xffffffff)
        self.assertEqual(result64, 7)
        self.assertEqual(result64 & 0xffffffff, result32)

    def test_mips64_daddiu_signed_immediate_wrap_and_zero_register(self):
        # DADDIU sign-extends its immediate and wraps modulo 2^64 without trapping.
        for value, delta in ((0x0123456789abcdef, 5),
                             (0x7fffffffffffffff, 1),
                             (0xffffffffffffffff, 1),
                             (0x8000000000000000, -32768)):
            emu = self.engine([daddiu(2, 4, delta), 0x03e00008, 0], mode='mips64')
            self.assertEqual(emu.call(0x100000, 0x10000c, value),
                             (value + delta) & 0xffffffffffffffff)

        emu = self.engine([daddiu(2, 0, -1), 0x03e00008, 0], mode='mips64')
        self.assertEqual(emu.call(0x100000, 0x10000c), 0xffffffffffffffff)
        emu = self.engine([daddiu(0, 4, 1), daddiu(2, 0, 0x1234),
                           0x03e00008, 0], mode='mips64')
        self.assertEqual(emu.call(0x100000, 0x100010, 0xffffffffffffffff), 0x1234)

    def test_mips64_daddiu_branch_and_return_delay_slots(self):
        # The delay-slot addition executes regardless of the branch outcome.
        words = [0x10850002,             # beq a0,a1,+2
                 daddiu(8, 7, -2),        # delay: t0 = a3 - 2
                 0xacc80000,              # skipped on taken branch
                 0xacc80004,              # runs on either path
                 0x03e00008, daddiu(2, 8, 1)]  # JR RA; v0=t0+1 in delay slot
        emu = self.engine(words, mode='mips64')
        value = 0x8123456789abcdef
        for left, right, first_store, steps in ((7, 7, False, 5), (7, 8, True, 6)):
            emu.write(0x200000, bytes(8))
            expected = (value - 1) & 0xffffffffffffffff
            self.assertEqual(emu.call(0x100000, 0x100018, left, right,
                                      0x200000, value), expected)
            written = emu.read(0x200000, 8)
            self.assertEqual(written[:4] != bytes(4), first_store)
            self.assertEqual(written[4:], struct.pack('<I', (value - 2) & 0xffffffff))
            self.assertEqual(emu.steps, steps)

    def test_daddiu_is_opt_in_and_does_not_admit_other_doubleword_ops(self):
        word = daddiu(2, 4, -1)
        self.assertFalse(allowed(word, 'mips32'))
        self.assertTrue(allowed(word, 'mips64'))
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            self.engine([word, 0x03e00008, 0]).call(0x100000, 0x10000c, 2)
        dadd = (4 << 21) | (5 << 16) | (2 << 11) | 0x2c
        self.assertFalse(allowed(dadd, 'mips64'))
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            self.engine([dadd, 0x03e00008, 0], mode='mips64').call(
                0x100000, 0x10000c, 1, 2)

    def test_mips64_ld_sd_preserve_full_doublewords_and_signed_offsets(self):
        # LD sign-extends its 16-bit address offset; SD writes all 64 bits.
        words = [ld(8, 4, -8), sd(8, 5, 0), daddu(2, 8, 0),
                 0x03e00008, 0]
        emu = self.engine(words, mode='mips64')
        value = 0xfedcba9876543210
        emu.write(0x200000, struct.pack('<Q', value))
        self.assertEqual(emu.call(0x100000, 0x100014, 0x200008, 0x201000), value)
        self.assertEqual(emu.read(0x201000, 8), struct.pack('<Q', value))
        self.assertEqual(emu.read_addresses, set(range(0x200000, 0x200008)))
        self.assertEqual(emu.written, set(range(0x201000, 0x201008)))
        self.assertEqual(emu.steps, 5)

    def test_mips64_ld_destination_alias_and_zero_register_memory_semantics(self):
        # LD A0,0(A0) reads the old base before replacing it; SD/LD on ZERO
        # still access memory even though the load result is discarded.
        alias = self.engine([ld(4, 4, 0), daddu(2, 4, 0),
                             0x03e00008, 0], mode='mips64')
        alias.write(0x200000, struct.pack('<Q', 0x8123456789abcdef))
        self.assertEqual(alias.call(0x100000, 0x100010, 0x200000),
                         0x8123456789abcdef)
        self.assertEqual(alias.read_addresses, set(range(0x200000, 0x200008)))

        zero = self.engine([sd(0, 4, 0), ld(0, 4, 0), daddu(2, 5, 0),
                            0x03e00008, 0], mode='mips64')
        self.assertEqual(zero.call(0x100000, 0x100014, 0x200000,
                                   0xdeadbeef01234567),
                         0xdeadbeef01234567)
        self.assertEqual(zero.read(0x200000, 8), bytes(8))
        self.assertEqual(zero.read_addresses, set(range(0x200000, 0x200008)))
        self.assertEqual(zero.written, set(range(0x200000, 0x200008)))

    def test_mips64_ld_sd_allow_ram_edges_and_reject_bad_addresses(self):
        edge_load = self.engine([ld(2, 4, 0), 0x03e00008, 0], mode='mips64')
        edge_load.write(0x02000000 - 8, struct.pack('<Q', 0x0123456789abcdef))
        self.assertEqual(edge_load.call(0x100000, 0x10000c, 0x02000000 - 8),
                         0x0123456789abcdef)

        edge_store = self.engine([sd(4, 5, 0), 0x03e00008, 0], mode='mips64')
        self.assertEqual(edge_store.call(0x100000, 0x10000c, 0x8877665544332211,
                                         0x02000000 - 8), 0)
        self.assertEqual(edge_store.read(0x02000000 - 8, 8),
                         struct.pack('<Q', 0x8877665544332211))

        for word, base in ((ld(2, 4, 0), 0x200001),
                           (sd(4, 5, 0), 0x200001),
                           (ld(2, 4, 0), 0x02000000),
                           (sd(4, 5, 0), 0x02000000),
                           (ld(2, 4, -8), 0x0000000000000004),
                           (sd(4, 5, 0), 0xfffffffffffffff8)):
            emu = self.engine([word, 0x03e00008, 0], mode='mips64')
            args = [0x200000, 0x200000]
            args[((word >> 21) & 31) - 4] = base
            with self.assertRaisesRegex(Unsupported, 'unaligned|outside RAM'):
                emu.call(0x100000, 0x10000c, *args)

    def test_mips64_sd_runs_in_taken_and_untaken_branch_delay_slot(self):
        # SD is the BEQ delay instruction and must execute on both paths.
        emu = self.engine([0x10850003, sd(6, 7, 0), 0x24020001, 0,
                           0x24020000, 0x03e00008, 0], mode='mips64')
        for left, right in ((5, 5), (5, 6)):
            emu.write(0x200000, bytes(8))
            self.assertEqual(emu.call(0x100000, 0x10001c, left, right,
                                      0x0123456789abcdef, 0x200000), 0)
            self.assertEqual(emu.read(0x200000, 8),
                             struct.pack('<Q', 0x0123456789abcdef))
            self.assertEqual(emu.written, set(range(0x200000, 0x200008)))

    def test_mips64_sd_delay_slot_can_branch_directly_to_return(self):
        # Unicorn's write hook faults on a taken branch whose SD delay slot
        # lands directly on JR RA. The backend temporarily tracks that one SD
        # from its decoded address while Unicorn executes both instructions.
        emu = self.engine([0x10850002, sd(6, 7, 0), 0x24020001,
                           0x03e00008, 0], mode='mips64')
        for left, right, expected in ((5, 5, 0), (5, 6, 1)):
            emu.write(0x200000, bytes(8))
            self.assertEqual(emu.call(0x100000, 0x100014, left, right,
                                      0x0123456789abcdef, 0x200000), expected)
            self.assertEqual(emu.read(0x200000, 8),
                             struct.pack('<Q', 0x0123456789abcdef))
            self.assertEqual(emu.written, set(range(0x200000, 0x200008)))
            self.assertEqual(emu.steps, 4 if left == right else 5)

    def test_mips64_write_observer_resumes_after_sd_delay_slot(self):
        emu = self.engine([0x10850002, sd(6, 7, 0), 0x24020001,
                           (43 << 26) | (7 << 21) | (6 << 16) | 8,
                           0x03e00008, 0], mode='mips64')
        emu.call(0x100000, 0x100018, 5, 5, 0x0123456789abcdef, 0x200000)
        self.assertEqual(emu.read(0x200000, 8),
                         struct.pack('<Q', 0x0123456789abcdef))
        self.assertEqual(emu.read(0x200008, 4), struct.pack('<I', 0x89abcdef))
        self.assertEqual(emu.written, set(range(0x200000, 0x20000c)))

    def test_ld_sd_are_opt_in_and_other_doubleword_memory_ops_stay_unsupported(self):
        for word in (ld(2, 4, 0), sd(2, 4, 0)):
            self.assertFalse(allowed(word, 'mips32'))
            self.assertTrue(allowed(word, 'mips64'))
            with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
                self.engine([word, 0x03e00008, 0]).call(
                    0x100000, 0x10000c, 0x200000, 0x200008)

        for op in (26, 27, 44, 45, 52, 60):  # LDL/LDR/SDL/SDR/LLD/SCD
            word = (op << 26) | (4 << 21) | (2 << 16)
            self.assertFalse(allowed(word, 'mips64'))
            with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
                self.engine([word, 0x03e00008, 0], mode='mips64').call(
                    0x100000, 0x10000c, 0x200000)

    def test_mips64_shift_executes_in_a_control_transfer_delay_slot(self):
        # JR RA; its delay slot computes DSRL32 V0,A0,31.
        emu = self.engine([0x03e00008, self.dshift(4, 2, 31, 0x3e)], mode='mips64')
        self.assertEqual(emu.call(0x100000, 0x100008, 0x8000000000000000), 1)
        self.assertEqual(emu.steps, 2)

    def test_mips64_cli_is_explicit_and_reports_full_return(self):
        data = elf([self.dshift(4, 2, 31, 0x3e), 0x03e00008, 0])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elf_path = root / 'synthetic.elf'
            case_path = root / 'case.json'
            elf_path.write_bytes(data)
            case_path.write_text(json.dumps({'start': '0x100000', 'end': '0x10000c',
                                             'args': ['0x8000000000000000']}))
            command = [sys.executable, str(Path(__file__).resolve().parents[1] /
                                           'tools' / 'emu' / 'ps2_emu.py'),
                       str(elf_path), hashlib.sha256(data).hexdigest(), str(case_path),
                       '--mips64']
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual(report['return_u32'], 1)
            self.assertEqual(report['return_u64'], 1)

    def test_mips64_is_opt_in_and_other_doubleword_ops_stay_unsupported(self):
        dsll32 = self.dshift(4, 2, 30, 0x3c)
        dsra32 = self.dshift(4, 2, 30, 0x3f)
        self.assertFalse(allowed(dsll32, 'mips32'))
        self.assertTrue(allowed(dsll32, 'mips64'))
        self.assertFalse(allowed(self.dshift(4, 2, 30, 0x3c, rs=1), 'mips64'))
        self.assertFalse(allowed(dsra32, 'mips64'))
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            self.engine([dsll32, 0x03e00008, 0]).call(0x100000, 0x10000c, 2)
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            self.engine([dsra32, 0x03e00008, 0], mode='mips64').call(
                0x100000, 0x10000c, 2)
        with self.assertRaisesRegex(Unsupported, 'mode must'):
            self.engine([0x03e00008, 0], mode='mips128')

    def test_repeated_calls_restore_cpu_state(self):
        emu = self.engine([0x00851021, 0xacc20000, 0x03e00008, 0x24420001])
        for value in range(8):
            self.assertEqual(emu.call(0x100000, 0x100010, value, 13, 0x200000), value + 14)
            self.assertEqual(emu.read(0x200000, 4), struct.pack('<I', value + 13))
            self.assertEqual(emu.steps, 4)

    def test_unsupported_fpu(self):
        emu = self.engine([0x46000000, 0x03e00008, 0])
        with self.assertRaisesRegex(Unsupported, 'unsupported instruction'):
            emu.call(0x100000, 0x10000c)

    def test_unsupported_mmi(self):
        emu = self.engine([0x70000000, 0x03e00008, 0])
        with self.assertRaises(Unsupported):
            emu.call(0x100000, 0x10000c)

    def test_rejects_r2_rotation_encoding(self):
        self.assertFalse(allowed(0x00221042))

    def test_branch_and_its_delay_slot(self):
        # BEQ A0,A1 skips one ADDIU but executes the branch delay slot.
        emu = self.engine([0x10850002, 0x24020007, 0x24420005, 0x03e00008, 0])
        self.assertEqual(emu.call(0x100000, 0x100014, 1, 1), 7)
        self.assertEqual(emu.call(0x100000, 0x100014, 1, 2), 12)

    def test_budget(self):
        emu = self.engine([0x1000ffff, 0])
        with self.assertRaisesRegex(Unsupported, 'budget'):
            emu.call(0x100000, 0x100008, budget=16)

    def test_escape(self):
        emu = self.engine([0x10000002, 0, 0, 0])
        with self.assertRaisesRegex(Unsupported, 'escaped'):
            emu.call(0x100000, 0x100008)

    def test_mmio(self):
        emu = self.engine([0x8c820000, 0x03e00008, 0])
        with self.assertRaisesRegex(Unsupported, 'execution failed'):
            emu.call(0x100000, 0x10000c, 0x10000000)

    def test_records_data_reads(self):
        emu = self.engine([0x8c820000, 0x03e00008, 0])
        emu.write(0x200000, struct.pack('<I', 123))
        self.assertEqual(emu.call(0x100000, 0x10000c, 0x200000), 123)
        self.assertEqual(emu.read_addresses, set(range(0x200000, 0x200004)))

    def test_bad_hash_and_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bad.elf'
            path.write_bytes(b'bad')
            with self.assertRaisesRegex(Unsupported, 'SHA-256'):
                Emu(path, '0' * 64)
            with self.assertRaisesRegex(Unsupported, 'ELF32'):
                Emu(path, hashlib.sha256(b'bad').hexdigest())

    def test_missing_input(self):
        with self.assertRaises(FileNotFoundError):
            Emu('/no-such-input-for-gameframe.elf', '0' * 64)


if __name__ == '__main__':
    unittest.main()

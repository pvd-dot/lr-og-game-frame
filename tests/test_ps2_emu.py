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

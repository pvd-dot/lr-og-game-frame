#!/usr/bin/env python3
"""Synthetic tests for the bounded PS2 SIF1 RAM data-plane helper."""

import ctypes
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
EE_SIZE = 0x02000000
IOP_SIZE = 0x00200000


class Result(ctypes.Structure):
    _fields_ = [
        ("tags_read", ctypes.c_uint32),
        ("packets_copied", ctypes.c_uint32),
        ("payload_bytes_copied", ctypes.c_uint32),
        ("final_stop_requested", ctypes.c_uint32),
    ]


class Sif1DataPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="gf-sif1-")
        library = pathlib.Path(cls.tmp.name) / "libsif1.so"
        compiler = os.environ.get("CC", "cc")
        subprocess.run(
            [
                compiler,
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-shared",
                "-fPIC",
                str(ROOT / "tools/emu/ps2_sif1_data.c"),
                "-o",
                str(library),
            ],
            check=True,
        )
        cls.lib = ctypes.CDLL(str(library))
        cls.ee = (ctypes.c_uint8 * EE_SIZE)()
        cls.iop = (ctypes.c_uint8 * IOP_SIZE)()
        cls.fn = cls.lib.gf_ps2_sif1_copy_data
        cls.fn.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.POINTER(Result),
        ]
        cls.fn.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def put(self, ram, address, data):
        ctypes.memmove(ctypes.addressof(ram) + address, data, len(data))

    def get(self, ram, address, size):
        return ctypes.string_at(ctypes.addressof(ram) + address, size)

    def le32(self, value):
        return int(value).to_bytes(4, "little")

    def clear_case(self):
        # Only the synthetic descriptors and destinations are touched by these
        # cases; clear their complete bounded regions without rewriting 34 MiB.
        ctypes.memset(ctypes.addressof(self.ee) + 0x100, 0, 0x5000)
        ctypes.memset(ctypes.addressof(self.iop) + 0x1000, 0xA5, 0x1000)

    def tag(self, address, tag_id, qwc, source):
        self.put(
            self.ee,
            address,
            self.le32((tag_id << 28) | qwc)
            + self.le32(source)
            + self.le32(0)
            + self.le32(0),
        )

    def header(self, data, words):
        return self.le32(data) + self.le32(words) + bytes(8)

    def make_valid_stream(self, stop=True):
        self.clear_case()
        payload_a = bytes((i * 7 + 3) & 0xFF for i in range(32))
        payload_b = bytes((i * 11 + 5) & 0xFF for i in range(16))
        second_data = 0x2000 | (0xC0000000 if stop else 0)
        # The first REF ends halfway through packet one's payload. The next REF
        # contributes that remainder and packet two; REFE ends the tag chain.
        self.put(self.ee, 0x2000, self.header(0x1000, 8) + payload_a[:16])
        self.put(
            self.ee,
            0x3000,
            payload_a[16:] + self.header(second_data, 4) + payload_b,
        )
        self.tag(0x100, 3, 2, 0x2000)
        self.tag(0x110, 3, 3, 0x3000)
        self.tag(0x120, 0, 0, 0x4000)
        return payload_a, payload_b

    def invoke(self, result=None, ee=None, iop=None, ee_size=EE_SIZE,
               iop_size=IOP_SIZE, first=0x100, max_tags=3):
        result = result or Result(0x11111111, 0x22222222, 0x33333333, 0x44444444)
        ee = self.ee if ee is None else ee
        iop = self.iop if iop is None else iop
        status = self.fn(ee, ee_size, iop, iop_size, first, max_tags,
                         ctypes.byref(result))
        return status, result

    def assert_refused_without_mutation(self, *, edit=None, first=0x100,
                                        max_tags=3, ee_size=EE_SIZE,
                                        iop_size=IOP_SIZE):
        payload_a, payload_b = self.make_valid_stream()
        del payload_a, payload_b
        if edit is not None:
            edit()
        result = Result(0x11111111, 0x22222222, 0x33333333, 0x44444444)
        before_result = bytes(result)
        before_iop = self.get(self.iop, 0x1000, 0x1000)
        before_ee = self.get(self.ee, 0x100, 0x5000)
        status, result = self.invoke(result=result, first=first, max_tags=max_tags,
                                     ee_size=ee_size, iop_size=iop_size)
        self.assertEqual(status, 0)
        self.assertEqual(bytes(result), before_result)
        self.assertEqual(self.get(self.iop, 0x1000, 0x1000), before_iop)
        self.assertEqual(self.get(self.ee, 0x100, 0x5000), before_ee)

    def test_split_ref_stream_copies_two_packets_and_reports_stop(self):
        payload_a, payload_b = self.make_valid_stream()
        result = Result()
        status, result = self.invoke(result)
        self.assertEqual(status, 1)
        self.assertEqual(
            (result.tags_read, result.packets_copied,
             result.payload_bytes_copied, result.final_stop_requested),
            (3, 2, 48, 1),
        )
        self.assertEqual(self.get(self.iop, 0x1000, 32), payload_a)
        self.assertEqual(self.get(self.iop, 0x2000, 16), payload_b)
        self.assertEqual(self.get(self.iop, 0x1020, 16), bytes([0xA5]) * 16)

    def test_finite_exact_stream_without_stop_is_data_copy_only(self):
        payload_a, payload_b = self.make_valid_stream(stop=False)
        result = Result()
        status, result = self.invoke(result)
        self.assertEqual(status, 1)
        self.assertEqual(result.final_stop_requested, 0)
        self.assertEqual(result.payload_bytes_copied, 48)
        self.assertEqual(self.get(self.iop, 0x1000, 32), payload_a)
        self.assertEqual(self.get(self.iop, 0x2000, 16), payload_b)

    def test_rejects_unsupported_tag_or_unterminated_chain_without_writes(self):
        self.assert_refused_without_mutation(edit=lambda: self.put(
            self.ee, 0x100, self.le32((1 << 28) | 2)
            + self.le32(0x2000) + bytes(8)))
        self.assert_refused_without_mutation(edit=lambda: self.put(
            self.ee, 0x100, self.le32((0xB << 28) | 2)
            + self.le32(0x2000) + bytes(8)))
        self.assert_refused_without_mutation(max_tags=2)

    def test_rejects_unaligned_source_and_malformed_word_count(self):
        self.assert_refused_without_mutation(edit=lambda: self.put(
            self.ee, 0x104, self.le32(0x2001)))
        self.assert_refused_without_mutation(edit=lambda: self.put(
            self.ee, 0x2004, self.le32(5)))

    def test_rejects_truncated_payload_and_out_of_range_destination(self):
        def oversized_payload():
            self.put(self.ee, 0x2004, self.le32(0x100))

        self.assert_refused_without_mutation(edit=oversized_payload)

        def bad_destination():
            self.put(self.ee, 0x2000, self.header(0x00200000, 8)
                     + self.get(self.ee, 0x2010, 16))

        self.assert_refused_without_mutation(edit=bad_destination)

        def unaligned_destination():
            self.put(self.ee, 0x2000, self.header(0x1001, 8)
                     + self.get(self.ee, 0x2010, 16))

        self.assert_refused_without_mutation(edit=unaligned_destination)

    def test_late_invalid_packet_does_not_apply_earlier_valid_packet(self):
        def bad_second_destination():
            self.put(self.ee, 0x3010, self.header(0x00200000, 4))

        self.assert_refused_without_mutation(edit=bad_second_destination)

    def test_rejects_ee_source_span_out_of_range_without_mutation(self):
        def source_past_end():
            self.put(self.ee, 0x104, self.le32(EE_SIZE - 16))

        self.assert_refused_without_mutation(edit=source_past_end)

    def test_rejects_early_stop_and_trailing_stream_bytes(self):
        def early_stop():
            self.put(self.ee, 0x2000, self.header(0xC0001000, 8)
                     + self.get(self.ee, 0x2010, 16))

        self.assert_refused_without_mutation(edit=early_stop)

    def test_rejects_short_ram_and_host_buffer_aliases_without_writes(self):
        self.assert_refused_without_mutation(ee_size=EE_SIZE - 1)
        result = Result(1, 2, 3, 4)
        before_result = bytes(result)
        before = self.get(self.ee, 0x1000, 0x100)
        status = self.fn(self.ee, EE_SIZE, self.ee, IOP_SIZE, 0x100, 3,
                         ctypes.byref(result))
        self.assertEqual(status, 0)
        self.assertEqual(bytes(result), before_result)
        self.assertEqual(self.get(self.ee, 0x1000, 0x100), before)

        result_alias = ctypes.cast(ctypes.addressof(self.ee) + 0x600,
                                   ctypes.POINTER(Result))
        before_ee = self.get(self.ee, 0x100, 0x5000)
        status = self.fn(self.ee, EE_SIZE, self.iop, IOP_SIZE, 0x100, 3,
                         result_alias)
        self.assertEqual(status, 0)
        self.assertEqual(self.get(self.ee, 0x100, 0x5000), before_ee)

    def test_rejects_misaligned_result_without_touching_any_buffer(self):
        self.make_valid_stream()
        raw_result = (ctypes.c_uint8 * (ctypes.sizeof(Result) + 1))(
            *([0x5A] * (ctypes.sizeof(Result) + 1)))
        result_alias = ctypes.cast(ctypes.addressof(raw_result) + 1,
                                   ctypes.POINTER(Result))
        before_result = bytes(raw_result)
        before_ee = self.get(self.ee, 0x100, 0x5000)
        before_iop = self.get(self.iop, 0x1000, 0x1000)
        status = self.fn(self.ee, EE_SIZE, self.iop, IOP_SIZE, 0x100, 3,
                         result_alias)
        self.assertEqual(status, 0)
        self.assertEqual(bytes(raw_result), before_result)
        self.assertEqual(self.get(self.ee, 0x100, 0x5000), before_ee)
        self.assertEqual(self.get(self.iop, 0x1000, 0x1000), before_iop)


if __name__ == "__main__":
    unittest.main()

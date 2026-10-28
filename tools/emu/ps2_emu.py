#!/usr/bin/env python3
"""Execute a restricted integer leaf function from a little-endian PS2 ELF.

Usage: ps2_emu.py ELF SHA256 CASE.json
CASE contains start/end (exclusive), up to four integer args, memory entries
{address, hex}, and outputs {address, size}. Integers may be JSON numbers or
0x strings. Prints the low 32 return bits, all written addresses and outputs.
Use outputs with tools/verify/bitcompare.py; this never compares code bytes.

This is a common MIPS32 instruction subset under Unicorn, NOT an R5900 machine.
No FPU, MMI, VU, general 64/128-bit operations, calls, syscalls, MMIO or
interrupts. DADDU is supported only as a low-word register move with one source
equal to zero, and not in a delay slot. Only use routines whose relevant state
fits 32-bit integer registers and RAM. Unlisted instructions, escaped control
flow and exhausted budgets fail closed.
State supplied by the caller is synthetic, not a captured running PS2 session.
"""
import hashlib
import json
import struct
import sys
from pathlib import Path

from unicorn import Uc, UcError, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
from unicorn import UC_HOOK_CODE, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn.mips_const import UC_MIPS_REG_0, UC_MIPS_REG_A0, UC_MIPS_REG_V0
from unicorn.mips_const import UC_MIPS_REG_SP, UC_MIPS_REG_RA, UC_MIPS_REG_PC

RAM_SIZE = 0x02000000
RETURN = 0x01fff000
STACK = 0x01ffe000


class Unsupported(ValueError):
    """The input or executed path is outside this backend's contract."""


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def segments(data):
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01':
        raise Unsupported('expected ELF32 little endian')
    h = struct.unpack_from('<16sHHIIIIIHHHHHH', data)
    if h[1] != 2 or h[2] != 8 or h[9] != 32:
        raise Unsupported('expected executable MIPS ELF with 32-byte program headers')
    result = []
    for i in range(h[10]):
        off = h[5] + i * h[9]
        if off + 32 > len(data):
            raise Unsupported('truncated program header')
        kind, offset, addr, _, filesz, memsz, flags, _ = struct.unpack_from('<8I', data, off)
        if kind != 1:
            continue
        if filesz > memsz or offset + filesz > len(data) or addr + memsz > RAM_SIZE:
            raise Unsupported('invalid or non-RAM segment')
        result.append((addr, data[offset:offset + filesz], flags))
    if not result:
        raise Unsupported('no loadable segments')
    return result


def allowed(word):
    op = word >> 26
    if op == 0:
        # Shifts, word arithmetic, bitwise, comparisons; only JR RA may return.
        if word & 63 == 8:
            return word == 0x03e00008
        fn = word & 63
        if fn in (0, 2, 3):
            return (word >> 21) & 31 == 0  # Exclude MIPS32r2 ROTR and reserved forms.
        if fn == 0x2d:  # DADDU is supported only as a 32-bit register move.
            rs, rt = (word >> 21) & 31, (word >> 16) & 31
            return (word >> 6) & 31 == 0 and (rs == 0 or rt == 0)
        return fn in (4, 6, 7, 0x21, 0x23, 0x24, 0x25, 0x26, 0x27, 0x2a, 0x2b) and (word >> 6) & 31 == 0
    # BEQ/BNE/BLEZ/BGTZ, ADDIU, SLTI/U, logical immediates, LUI, byte/half/word RAM.
    if op in (6, 7) and (word >> 16) & 31:
        return False
    if op == 15 and (word >> 21) & 31:
        return False
    return op in (4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 32, 33, 35, 36, 37, 40, 41, 43)


class Emu:
    def __init__(self, elf, sha256):
        self.data = Path(elf).read_bytes()
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        if self.sha256 != sha256:
            raise Unsupported('ELF SHA-256 mismatch')
        self.segments = segments(self.data)
        self.uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        self.uc.mem_map(0, RAM_SIZE)
        for addr, contents, _ in self.segments:
            self.uc.mem_write(addr, contents)
        self.uc.hook_add(UC_HOOK_CODE, self._instruction)
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._write)
        self.uc.hook_add(UC_HOOK_MEM_READ, self._read)
        self.running = False
        self.initial_cpu = self.uc.context_save()

    def read(self, address, size):
        if address < 0 or size < 0 or address + size > RAM_SIZE:
            raise Unsupported('read outside RAM')
        return bytes(self.uc.mem_read(address, size))

    def write(self, address, data):
        if address < 0 or address + len(data) > RAM_SIZE:
            raise Unsupported('write outside RAM')
        self.uc.mem_write(address, data)

    def _instruction(self, uc, address, size, _):
        if not self.start <= address < self.end or size != 4:
            raise Unsupported(f'control flow escaped function at {address:#x}')
        self.steps += 1
        if self.steps > self.budget:
            raise Unsupported('instruction budget exhausted')
        word = struct.unpack('<I', self.read(address, 4))[0]
        if not allowed(word):
            raise Unsupported(f'unsupported instruction {word:#010x} at {address:#x}')
        in_delay_slot = address == getattr(self, 'delay_slot_address', None)
        is_daddu = word >> 26 == 0 and word & 63 == 0x2d
        is_control = (word == 0x03e00008 or word >> 26 in (4, 5, 6, 7))
        if is_control and address + 4 < self.end:
            delay_word = struct.unpack('<I', self.read(address + 4, 4))[0]
            if delay_word >> 26 == 0 and delay_word & 63 == 0x2d and allowed(delay_word):
                self.hook_error = 'DADDU register move in a delay slot is unsupported'
                raise Unsupported(self.hook_error)
        if is_daddu:
            if in_delay_slot:
                raise Unsupported('DADDU register move in a delay slot is unsupported')
            rs, rt, rd = (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
            source = rt if rs == 0 else rs
            value = 0 if source == 0 else uc.reg_read(UC_MIPS_REG_0 + source)
            if rd:
                uc.reg_write(UC_MIPS_REG_0 + rd, value & 0xffffffff)
            # Unicorn's MIPS32 core does not execute this MIPS64 instruction.
            # Skip it after applying the only admitted low-word behavior.
            uc.reg_write(UC_MIPS_REG_PC, address + 4)
        self.delay_slot_address = address + 4 if is_control else None

    def _write(self, uc, access, address, size, value, _):
        if self.running:
            if not 0 <= address <= RAM_SIZE - size:
                raise Unsupported('non-RAM write')
            self.written.update(range(address, address + size))

    def _read(self, uc, access, address, size, value, _):
        if self.running:
            self.read_addresses.update(range(address, address + size))

    def call(self, start, end, *args, budget=10000):
        if len(args) > 4 or budget <= 0:
            raise Unsupported('requires <=4 integer register arguments and a positive budget')
        if start % 4 or end % 4 or not 0 <= start < end < STACK:
            raise Unsupported('invalid function bounds')
        if not any(flags & 1 and addr <= start < end <= addr + len(data)
                   for addr, data, flags in self.segments):
            raise Unsupported('function is not in one file-backed executable segment')
        # Restore hidden CPU state too, including MIPS delay-slot bookkeeping.
        # RAM persists: callers must restore all case inputs between calls.
        self.uc.context_restore(self.initial_cpu)
        for reg in range(1, 32):
            self.uc.reg_write(UC_MIPS_REG_0 + reg, 0)
        for i, value in enumerate(args):
            self.uc.reg_write(UC_MIPS_REG_A0 + i, value & 0xffffffff)
        self.uc.reg_write(UC_MIPS_REG_SP, STACK)
        self.uc.reg_write(UC_MIPS_REG_RA, RETURN)
        self.start, self.end, self.budget = start, end, budget
        self.steps, self.written = 0, set()
        self.read_addresses = set()
        self.delay_slot_address = None
        self.hook_error = None
        self.running = True
        try:
            self.uc.emu_start(start, RETURN, timeout=1000000)
        except UcError as e:
            if self.hook_error:
                raise Unsupported(self.hook_error) from e
            raise Unsupported(f'Unicorn execution failed: {e}') from e
        finally:
            self.running = False
        if self.uc.reg_read(UC_MIPS_REG_PC) != RETURN:
            raise Unsupported('function did not return before timeout')
        return self.uc.reg_read(UC_MIPS_REG_V0) & 0xffffffff


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    emu = Emu(sys.argv[1], sys.argv[2])
    case = json.loads(Path(sys.argv[3]).read_text())
    for region in case.get('memory', []):
        emu.write(number(region['address']), bytes.fromhex(region['hex']))
    result = emu.call(number(case['start']), number(case['end']),
                      *(number(a) for a in case.get('args', [])))
    print(json.dumps({'return_u32': result, 'instructions': emu.steps,
                      'read_addresses': sorted(emu.read_addresses),
                      'written_addresses': sorted(emu.written),
                      'outputs': [emu.read(number(r['address']), number(r['size'])).hex()
                                  for r in case.get('outputs', [])]}))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Unsupported, OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(f'unavailable/unsupported: {e}', file=sys.stderr)
        sys.exit(2)

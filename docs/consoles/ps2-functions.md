# PS2 function verification

This fork adds a small backend to GameFrame's existing `re-trace` → `re-emulate` →
`re-port` → `re-verify` workflow. It compares **behavior**, not compiled code bytes.
Original instructions execute under Unicorn; reconstructed code runs on the host.
`tools/verify/bitcompare.py` compares their resulting raw state without changes.

## NixOS setup

```sh
nix develop
python tests/test_ps2_emu.py
```

The flake pins Python, Unicorn and the native compiler, plus tools needed by the
upstream tests. No DOS installation, PS2 SDK or historical compiler is needed for
this backend. DOS and Windows support remain available upstream features.

## First run

Trace the function first: identify its bounds, inputs, memory layout and callees.
Choose an integer leaf routine for this backend. Keep original executables,
reconstructed game code, dumps and ground truth outside this public framework.
The ELF's SHA-256 is mandatory; wrong or missing inputs fail.

```python
import struct
import sys
sys.path.insert(0, 'tools/emu')
from ps2_emu import Emu

emu = Emu('/external/game.elf', 'the verified executable SHA-256')
emu.write(0x00800000, struct.pack('<I', 123))
# Substitute the traced function's actual bounds and arguments.
result = emu.call(start, end, 0x00800000)
state = emu.read(0x00800000, 4)
print(result, state.hex(), sorted(emu.written))
```

CPU state resets between calls. RAM persists: restore every input and any state
changed by the previous call, or use a fresh `Emu` per independent case. `written`
includes every byte written by the original during the last call. Compare all
relevant state, and reject unexpected writes outside the modeled objects.
`read_addresses` records data reads. Unspecified RAM starts zeroed; supply and
justify all state read by the function rather than treating those zeros as a
capture of a running game.
The CLI accepts one JSON case; `python tools/emu/ps2_emu.py --help` prints its format.

Run reconstructed C/C++ with a native compiler, or call a reconstruction in another
language. Give it the same inputs and serialize its state in the original layout.
Use the existing comparator:

```sh
python tools/verify/bitcompare.py u32 /external/expected.bin /external/actual.bin
```

Require a positive case count and the expected dump sizes. Include boundaries,
aliases and random inputs within the known caller domain. Also test a deliberately
incorrect reconstruction: the comparison must fail. Keep the input generator,
seed, input hash, candidate source, compiler command, backend revision, dependency
versions and outputs with the report. Label synthetic inputs as synthetic.

## Last Raven pilot

The first local experiment executes USA `SLUS_213.38`, SHA-256
`a2094bee85cbdf069d1c5afee39013e3dfd8633ef19972cbf1456e284c682dea`,
at `[0x001e9820, 0x001e9844)`. Its observed operations store an input word at a
counter-selected word slot, then increment the counter. The nominal purpose and
real caller domain remain unconfirmed.

10,000 synthetic states exercise indices 0–31, 32-bit input values and input aliases
to the destination or counter. Native C compiled with GCC `-O2` agrees on all
650,000 compared 32-bit words (the entire 256-byte object plus the external input
word per case). A counter-increment-by-two mutant differs in all 10,000 cases.
The existing GameFrame comparator accepts the candidate and rejects the mutant.
The void routine's unspecified return register is not compared.

Game-specific source, generator and raw reports stay in the external lr-og data
directory: `$HOME/.local/share/lr-og/persistent/game-frame/first-function/`.
`run.py FRAMEWORK ELF CANDIDATE_C OUTPUT_DIR` reproduces the experiment inside
`nix develop`; it reads the existing lr-og `main_001e9820.c` candidate. No compiler
byte comparison or historical compiler is used. This is agreement for these inputs,
not a proof for all inputs or a demonstration of running gameplay.

## Supported boundary

`ps2_emu.py` loads little-endian ELF32 MIPS executable segments into 32 MiB of RAM.
Its default is the listed common MIPS32 integer subset: word arithmetic, shifts,
logic, comparisons, ordinary conditional branches, byte/halfword/word
loads/stores and `jr ra`. It also admits `DADDU` only when one source register is
`$zero`; this copies the modeled register value. In MIPS32 mode that is the low
word. In optional MIPS64 mode it is the full doubleword. This narrow `DADDU`
form is rejected in a delay slot because the hook cannot safely preserve its
scheduled execution. Other `DADDU` arithmetic is unsupported. Other supported
instructions in delay slots execute. Both modes support at most four register
arguments.

Pass `mode='mips64'` to `Emu` or add `--mips64` to the CLI to opt into Unicorn's
64-bit MIPS register model. This mode adds only `DSLL32` and `DSRL32`; these
execute as 64-bit doubleword shifts with shift amounts `32 + sa`, as specified
by the [MIPS64 instruction set reference](https://s3-eu-west-1.amazonaws.com/downloads-mips/documents/MD00087-2B-MIPS64BIS-AFP-05.04.pdf).
All other 64/128-bit operations remain unsupported. The mode returns a 64-bit
value and reports `return_u64` in CLI output, while retaining `return_u32` as its
low word. Inputs are full 64-bit register values in this mode. A 32-bit signed
argument must be passed sign-extended when the original caller does so; this
tool does not infer ABI extension rules.

Unicorn's generic MIPS CPU is **not a complete R5900/PS2 model**. The optional
mode uses MIPS64 semantics for the two admitted shifts; this is not evidence that
the generic CPU models all R5900 behavior. This backend does not support other
64/128-bit operations, MMI, FPU, VU, syscalls, callees, MMIO, interrupts, timing
or hardware side effects. Unsupported instructions, escaped control flow,
invalid accesses, timeouts and instruction-budget exhaustion fail; they never
become passing comparisons. The instruction allowlist is deliberately small,
not an automatic promise of PS2 equivalence for arbitrary input states.

Functions needing PS2-specific execution require an appropriate backend, such as
captures from a full PS2 emulator. Do not substitute generic IEEE floating point
or fabricated device results and describe those as original PS2 behavior. Keep
using GameFrame's tracing, state capture and output comparison strategy as the
execution backend expands.

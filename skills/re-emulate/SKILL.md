---
name: re-emulate
description: Run individual functions from a game's Windows x64 binaries (tools/emu/pe_emu.py), 32-bit x86 Windows binaries (tools/emu/pe32_emu.py) or 16-bit DOS/NE programs (tools/emu/dos_emu/) under the Unicorn emulator to capture exact ground truth for chosen inputs, and record the calls they make (random draws) with call_trace.py. Use for anything a reimplementation must match bit for bit - random number generators, maths, procedural generation, simulation steps, pathfinding, parsers - instead of trusting the pseudocode.
---

# Emulate original code for ground truth

For PS2 integer leaf routines, use `tools/emu/ps2_emu.py` and the exact setup,
input/output contract and limits in `docs/consoles/ps2-functions.md`. It loads a
hash-verified ELF and feeds the same behavioral verification workflow below.
It is not a full R5900 backend: unsupported PS2 instructions or hardware require
another execution backend, never guessed results. No compiler byte matching is
required for this fork's PS2 experiment.

Reading code tells you the shape of a computation; only running it tells you the exact result.
In a simulation, one bit of difference in a float or one random draw out of order grows until
the reimplementation's world no longer matches. The emulator runs a single original function,
many times, with inputs you choose, without starting the game.

## How the harness works

```python
import sys; sys.path.insert(0, 'tools/emu')
from pe_emu import Emu, F32
emu = Emu('game/Game.exe', ['game/Engine.dll'])  # loads, relocates, links imports by name
```

- Every module loads at its preferred base (relocated if two overlap). Imports between loaded
  modules are linked; any other import becomes a stub. Common C runtime and kernel32 calls have
  working stand-ins (`CRT_STUBS`: malloc, free, memset, memcpy, strlen, critical sections...).
  An import with no stand-in stops the run with its name: add one (`stubs={'Name': handler}`).
- `emu.call(addr, *args)` uses the Windows x64 convention: integers and pointers in rcx, rdx,
  r8, r9, floats wrapped in `F32(...)` or `F64(...)` in xmm0-3, the rest on the stack. It returns
  rax; `call_f32` returns xmm0, `call_i32` a signed int.
- Memory: `malloc(size)` (zeroed), `write(addr, bytes)`, `read(addr, n)`, `u8/u32/i32/u64`,
  `f32s(addr, n)`, `cstr(addr)`, `bytes_in(data)` (copy bytes in, get the address).
- Addresses: `export(module, name)`, or `va(module, 0x1400be770)` for a Ghidra address (it
  follows a relocated module).
- `hook_function(addr, name, handler)` replaces any function, original or imported, with Python:
  use it for callees that touch files, threads, the GPU, or that you want to observe.

## Steps

1. **Trace first** (`re-trace`): know the function's arguments, the layout of the objects it
   reads (offsets and types), the globals it needs (a singleton pointer at `DAT_...`), and its
   callees that must be stubbed.
2. **Build the inputs** in emulated memory: `malloc` each object, `struct.pack` the fields at
   their offsets, link pointers. Point globals at your objects with `write(va(...), pack('<Q', ptr))`.
   Build the smallest object graph that works; the emulator reports every invalid access with
   the address and the code position, which tells you the next field to fill.
3. **Write a generator script** (`tools/emu/<topic>.py` in the project, modelled on
   `examples/sample-game/ground_truth.py`): a docstring with usage and outputs; a fresh `Emu`
   per independent case; many inputs (every cell of a small map, seeds 1 to 50, edge cases such
   as zero, negatives and borders); raw little-endian outputs into `derived/emu/<topic>/`
   (ignored by git, because ground truth is derived from the game).
4. **Test one rule at a time.** In the port, move the rule out of its system into a function
   whose inputs are the fields the original reads. The generator script writes those fields;
   the port's test calls that function with the same values. Choose about a tenth of the cases at
   the edges: zero, values below zero, negative zero, NaN, lists out of order, empty lists.
   Record floats as their bits, so that NaN and negative zero survive the file. To check the
   draws without a call trace, compare the generator's state before and after the call, and
   record its next draw after the call. Both must match the port's.
5. **Record the draws** when the function consumes the game's random number generator:
   ```python
   from call_trace import CallTrace
   trace = CallTrace(emu, {emu.va('engine.dll', 0x180012340): 'Rand',
                           emu.va('engine.dll', 0x180012380): 'RandFloat'}, args=1)
   emu.call(target, ...)
   trace.write('derived/emu/ai/seed-1.trace')   # "Rand(10)<TAB>game.exe+0x9be55" per draw
   ```
   Only outermost calls are logged, so a float draw that calls the integer draw counts once.
6. **Compare** the reimplementation's output with `re-verify` (`bitcompare.py`, `seqdiff.py`).
   Not exact? Emulate intermediate functions separately and compare stage by stage, until the
   first stage that differs is small enough to read instruction by instruction.

## Pitfalls

- **Invalid memory access**: a pointer field you did not fill. Find which offset of which object
  from the reported code position and the disassembly there.
- **Type checks on objects you built**: a function that asks whether an object derives from a
  class (`IsDerivedFrom`, `dynamic_cast`) fails on a synthetic object. Hook the check to answer
  yes when the object is the type the code expects.
- **Engine constructors**: the engine's base object constructor may register the object and
  draw its id from a generator of its own. Hook it to return the object. Never count those draws
  as the game's.
- **Thread-local storage**: the harness maps a TEB at GS; code that uses `__declspec(thread)`
  variables also needs the TLS slot array (`TEB+0x58`) pointing at a block for the module.
- **Exceptions**: SEH and C++ exceptions do not unwind in the emulator; inputs that make the
  original throw will stop the run.
- **Long functions** (a whole map generator): pass `timeout_insns` and emulate them whole rather
  than porting blind; dump intermediate state with hooks to compare in stages.
- **DllMain is not run**: if a function depends on globals a DLL sets up at load, initialise
  them yourself, or call the module's initialiser explicitly.
- **32-bit games** (x86) run under `tools/emu/pe32_emu.py`: `Emu32('GAME.EXE')`, then
  `call(addr, args=[...])` for cdecl and stdcall, or `call(addr, esi=..., edi=...)` for
  hand-written routines that take registers. MMX and x87 work. Every import stops the run with
  its name until you give it a handler (`stubs=`). It has no TEB at FS and does not set `this`
  in ecx for you: pass `ecx=` for thiscall. `docs/ABI.md` lists the conventions, and
  `docs/hardware/simd.md` shows it on MMX code. `call(..., fpu_cw=0x037f)` loads an x87
  control word first (`NEAREST` 0x037f, `CHOP` 0x0f7f), and `emu.fpu_modes(addr, cases)` lists
  the cases whose result depends on the rounding mode. `call(..., until=ADDR)` stops at an
  address, such as a call to `sprintf` that needs the C runtime, and returns `stack`, the
  call's arguments (docs/knowhow/x87-magic-add-rounding.md). Linux ELF and macOS Mach-O
  binaries need a different loader; the calling convention is System V (rdi, rsi, rdx, rcx, r8,
  r9; floats in xmm0-7).
- **Floating point state**: the emulator starts with the default MXCSR (round to nearest). If the
  game changes the rounding mode or enables flush-to-zero, set it the same way.

## 16-bit DOS and NE programs: dos_emu

```python
import sys; sys.path.insert(0, 'tools/emu')
from dos_emu import DosEmu, Long, FarPtr
emu = DosEmu('game/GAME.EXE')                  # NE: 16-bit protected mode; MZ: real mode
emu.call('1000:873a', 5, Long(100000))            # Ghidra's address or an export name; AX
emu.call_long('RANDOM_LONG', 1000)                # DX:AX as one 32-bit value
p = emu.bytes_in(open('game/LEVEL.DAT', 'rb').read(512)); emu.call('LOADLEVEL', p, 512)
emu.u16(FarPtr(0x1048, 0x12de))                   # read a global afterwards
emu.trace({'1040:0a12': 'Random'}, args=1); emu.write_trace('derived/rng.trace')
```

- Addresses are Ghidra's: NE segment N is selector `1000 + 8 * (N - 1)`, MZ loads at `1000`.
  The emulator uses the same numbers, so pseudocode addresses work unchanged.
- Arguments go in the **Pascal** order by default (left to right, callee pops). An int is a
  word, `Long(v)` two words, `FarPtr`/(seg, off) a far pointer. `conv='cdecl'` for C code.
- A Pascal `var` parameter is a far pointer pushed segment first: pass the variable's
  address, as in `emu.call('1008:2026', 1, 0x1048, 0xa2f4, 0x1048, 0xa2ee)` for
  `P(n: Byte; var A, B)`. When the real callers always pass the same globals, port it
  with a choice between them (player 1 or 2), not a pointer.
- A procedure nested in another (it reads its parent's frame through `[bp + 4]`, the
  static link) cannot be called alone: call the parent with states that reach it.
- DS = SS = the auto data segment, as in a Pascal program; override with `regs={'ds': ...}`.
- An import or interrupt with no handler stops with its name and caller: add
  `stubs={'KERNEL.3': fn}` (handler returns the bytes to pop) or `interrupts={0x21: fn}`.
- **Drawing routines** (DOS games write straight into video memory): give the routine a
  screen with `screen = emu.new_segment()`, put its selector in the global the code loads into
  ES (`VgaScreenSeg`), fill the screen with a sentinel byte (0xEE) so skipped transparent
  pixels show, set the globals, call, and compare all 64 KB with the port's output. Load the
  data blocks it reads (`megadata`, sprite banks) the same way, built by the port's own loader,
  so one test checks the loader's layout and the renderer together.
- **Inline code inside a big procedure** (a main loop draws a layer itself):
  `emu.run_range('1000:cab1', '1000:ccac')` runs from one address until execution reaches the
  other. Pick a fragment that leaves the stack balanced and uses globals, not the enclosing
  procedure's locals (or set `bp` in `regs`). Near procedures end in `ret`: stop at its address.
  `run_range` starts on the start-up stack; to carry on a program that is running (the next
  frame), use `emu.run_to(address)`, which keeps every register.
- **The whole program, headless**: `tools/emu/dos_env/` adds DOS files, the BIOS, a DPMI
  host, timer interrupts and a key queue, so a DOS game starts and runs in dos_emu, frame by
  frame, with its memory open to you (docs/DOS.md section 5b). When it stops on something
  missing, add that to dos_env with a test, not a workaround in the game project.
  To save the state before and after chosen fragments of the main loop, frame after frame,
  use `tools/emu/fragments.py` (re-verify, "Capturing fragments of a main loop").
- For a function that needs DOS, timers, VGA or sound, run the whole program in DOSBox-X
  (`scripts/dosbox-run.sh`) and read memory with the debugger (`tools/dos/dbx_control.py`):
  docs/DOS.md sections 6 and 7.


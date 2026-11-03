"""Apply the IOP exception-entry effects selected by an already reached event test.

This is not an IOP scheduler or event runner. The caller must supply the live
64 KiB hardware image and CP0 values at an ``iopEventTest`` interrupt check.
The function only evaluates the source interrupt gate and, when selected,
returns the CP0/PC effects of ``psxException(0, 0)``. It does not mutate input.
"""

from __future__ import annotations

from typing import Any


HW_BYTES = 0x10000
IOP_ISTAT = 0x1070
IOP_IMASK = 0x1074
IOP_ICTRL = 0x1078
U32_MAX = 0xFFFFFFFF

STATUS_GATE_MASK = 0xFE01
STATUS_INTERRUPT_THRESHOLD = 0x0401
STATUS_BEV = 1 << 22
CAUSE_EXCEPTION_CODE_MASK = 0x7F
CAUSE_BD = 1 << 31
BEV_EXCEPTION_VECTOR = 0xBFC00180
NORMAL_EXCEPTION_VECTOR = 0x80000080


def _u32(name: str, value: Any) -> int:
    if type(value) is not int or not 0 <= value <= U32_MAX:
        raise ValueError(f"{name} must be an unsigned 32-bit integer")
    return value


def _read_u32_le(data: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def enter_iop_interrupt(
    iop_hw: bytes | bytearray,
    *,
    pc: int,
    status: int,
    cause: int,
    epc: int,
) -> dict[str, Any]:
    """Return the result of the IOP event-test interrupt-entry predicate.

    The caller is responsible for having reached the interrupt check in
    ``iopEventTest`` and for supplying hardware values after any preceding
    event callbacks. This function does not perform those callbacks, advance
    time, service counters, invoke the scheduler, execute the guest handler,
    or mutate ``iop_hw``.

    The source gate is ``ICTRL != 0 && (ISTAT & IMASK) != 0 &&
    (Status & 0xFE01) >= 0x401``. A taken gate applies ``psxException(0, 0)``:
    clear Cause bits 0..6 while preserving all higher bits (including BD), set
    EPC to the current PC, select the BEV or normal vector, and push Status
    bits 0..3 into bits 2..5 after clearing Status bits 0..5. Inputs outside this narrow
    register/image domain return ``refused`` without mutation.
    """
    if type(iop_hw) not in (bytes, bytearray) or len(iop_hw) != HW_BYTES:
        return _refused("IOP hardware must be exactly 64 KiB of bytes")

    try:
        pc = _u32("PC", pc)
        status = _u32("Status", status)
        cause = _u32("Cause", cause)
        epc = _u32("EPC", epc)
        if pc & 3:
            raise ValueError("PC must be word aligned")
    except ValueError as exc:
        return _refused(str(exc))

    ictrl = _read_u32_le(iop_hw, IOP_ICTRL)
    istat = _read_u32_le(iop_hw, IOP_ISTAT)
    imask = _read_u32_le(iop_hw, IOP_IMASK)

    if not (ictrl != 0 and (istat & imask) != 0 and
            (status & STATUS_GATE_MASK) >= STATUS_INTERRUPT_THRESHOLD):
        return {
            "disposition": "not_taken",
            "reason": "IOP interrupt-entry predicate is false",
            "pc": pc,
            "status": status,
            "cause": cause,
            "epc": epc,
        }

    next_status = (status & ~0x3F) | ((status & 0x0F) << 2)
    vector = BEV_EXCEPTION_VECTOR if status & STATUS_BEV else NORMAL_EXCEPTION_VECTOR
    return {
        "disposition": "taken",
        "reason": "",
        "pc": vector,
        "status": next_status & U32_MAX,
        "cause": cause & ~CAUSE_EXCEPTION_CODE_MASK,
        "epc": pc,
    }


def _refused(reason: str) -> dict[str, Any]:
    return {"disposition": "refused", "reason": reason}

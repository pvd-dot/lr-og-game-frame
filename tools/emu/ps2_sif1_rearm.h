/*
 * Bounded PCSX2 SIF1 receiver rearm after an IOP DMA10 start write.
 *
 * This helper does not perform the guest CHCR/BCR stores. The caller supplies
 * the resulting IOP/EE hardware images, serialized 0x244-byte `_sif` image,
 * and the explicit private `sif1_dma_stall` input. It models the selected
 * psxDma10 -> SIF1Dma path only for an empty FIFO, an idle EE SIF1 channel,
 * and a zero-QWC EE channel. It does not model data transfer, interrupts,
 * scheduling, or concurrent channel activity.
 *
 * Source: PCSX2 v2.8.2 fd9d310, IopDma.cpp::psxDma10 and Sif1.cpp::SIF1Dma,
 * Sif1Init, Sif1End. The static stall flag is not part of `_sif` savestates;
 * callers must supply its actual value. See the framework PS2 function guide.
 */
#ifndef GAMEFRAME_PS2_SIF1_REARM_H
#define GAMEFRAME_PS2_SIF1_REARM_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GF_PS2_SIF1_REARM_HW_SIZE 0x10000u
#define GF_PS2_SIF1_REARM_STATE_SIZE 0x244u

typedef enum gf_ps2_sif1_rearm_status {
	GF_PS2_SIF1_REARM_REFUSED = 0,
	GF_PS2_SIF1_REARM_COMPLETE = 1
} gf_ps2_sif1_rearm_status;

/* Private, non-savestate state read from the selected runtime environment. */
typedef struct gf_ps2_sif1_private_state {
	uint8_t dma_stall;
	uint8_t reserved[3];
} gf_ps2_sif1_private_state;

/*
 * Apply one bounded DMA10/SIF1 callback transition atomically on completion.
 *
 * `iop_hw` and `ee_hw` are exact 64 KiB hardware-register images; `sif1_state`
 * is the exact 0x244-byte serialized PCSX2 `_sif` object. They are the single
 * authority for public registers. The private input/output must be disjoint
 * from all images; private_in and private_out may be the same object.
 *
 * Expected caller state is after the guest has written DMA10 CHCR=0x41000300
 * and BCR low halfword=0x20, with DMA10 enabled in PCR2. The FIFO size is the
 * authoritative empty check for this path; read/write cursor words are not
 * consumed by it. The function models
 * psxDma10 setting IOP busy=1/end=0 before entering SIF1Dma. On a stalled
 * early return (dma_stall=1 and EE MADR > DMAC STADR, with QWC=0 and FIFO
 * empty), those busy/end writes commit, while cycle/SBUS/private-stall state
 * is preserved. Otherwise SIF1Init resets EE/IOP cycle counters, the private
 * stall flag is cleared, and Sif1End clears SBUS_F240 bits 0x4040.
 *
 * Unsupported inputs return REFUSED without modifying any image or
 * private_out. COMPLETE means only this bounded device callback transition.
 */
gf_ps2_sif1_rearm_status gf_ps2_sif1_empty_fifo_rearm(
	uint8_t *iop_hw, size_t iop_hw_size,
	uint8_t *ee_hw, size_t ee_hw_size,
	uint8_t *sif1_state, size_t sif1_state_size,
	const gf_ps2_sif1_private_state *private_in,
	gf_ps2_sif1_private_state *private_out);

#ifdef __cplusplus
}
#endif

#endif

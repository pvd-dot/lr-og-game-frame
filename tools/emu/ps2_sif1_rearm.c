#include "ps2_sif1_rearm.h"

#include <string.h>

enum {
	IOP_DMA10_BCR = 0x1534,
	IOP_DMA10_CHCR = 0x1538,
	IOP_DMA_PCR2 = 0x1570,
	EE_SIF1_MADR = 0xc410,
	EE_SIF1_QWC = 0xc420,
	EE_DMAC_STADR = 0xe060,
	EE_SBUS_F240 = 0xf240,
	SIF_FIFO_SIZE = 0x218,
	SIF_EE_BUSY = 0x21d,
	SIF_EE_CYCLES = 0x220,
	SIF_IOP_END = 0x224,
	SIF_IOP_BUSY = 0x225,
	SIF_IOP_CYCLES = 0x228,
};

_Static_assert(sizeof(gf_ps2_sif1_private_state) == 4, "private SIF1 state ABI");

static uint16_t load16(const uint8_t *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t load32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
		((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void store32(uint8_t *p, uint32_t value)
{
	p[0] = (uint8_t)value;
	p[1] = (uint8_t)(value >> 8);
	p[2] = (uint8_t)(value >> 16);
	p[3] = (uint8_t)(value >> 24);
}

static int span_end(const void *ptr, size_t size, uintptr_t *begin, uintptr_t *end)
{
	uintptr_t b;
	if (ptr == NULL || size == 0)
		return 0;
	b = (uintptr_t)ptr;
	if (size > UINTPTR_MAX - b)
		return 0;
	*begin = b;
	*end = b + size;
	return 1;
}

static int spans_overlap(uintptr_t a0, uintptr_t a1, uintptr_t b0, uintptr_t b1)
{
	return a0 < b1 && b0 < a1;
}

static int disjoint_inputs_and_outputs(
	const uint8_t *iop_hw, const uint8_t *ee_hw, const uint8_t *sif,
	const gf_ps2_sif1_private_state *private_in,
	const gf_ps2_sif1_private_state *private_out)
{
	uintptr_t lo[5], hi[5];
	const void *ptrs[5] = {iop_hw, ee_hw, sif, private_in, private_out};
	const size_t sizes[5] = {
		GF_PS2_SIF1_REARM_HW_SIZE, GF_PS2_SIF1_REARM_HW_SIZE,
		GF_PS2_SIF1_REARM_STATE_SIZE, sizeof(*private_in), sizeof(*private_out)
	};
	size_t i, j;

	for (i = 0; i < 5; ++i) {
		if (!span_end(ptrs[i], sizes[i], &lo[i], &hi[i]))
			return 0;
	}
	for (i = 0; i < 3; ++i) {
		for (j = i + 1; j < 3; ++j) {
			if (spans_overlap(lo[i], hi[i], lo[j], hi[j]))
				return 0;
		}
	}
	for (i = 0; i < 3; ++i) {
		if (spans_overlap(lo[i], hi[i], lo[3], hi[3]) ||
			spans_overlap(lo[i], hi[i], lo[4], hi[4]))
			return 0;
	}
	/* Exact in-place private input/output is safe because input is copied first. */
	if (private_in != private_out && spans_overlap(lo[3], hi[3], lo[4], hi[4]))
		return 0;
	return 1;
}

gf_ps2_sif1_rearm_status gf_ps2_sif1_empty_fifo_rearm(
	uint8_t *iop_hw, size_t iop_hw_size,
	uint8_t *ee_hw, size_t ee_hw_size,
	uint8_t *sif1_state, size_t sif1_state_size,
	const gf_ps2_sif1_private_state *private_in,
	gf_ps2_sif1_private_state *private_out)
{
	uint8_t next_iop_hw[GF_PS2_SIF1_REARM_HW_SIZE];
	uint8_t next_ee_hw[GF_PS2_SIF1_REARM_HW_SIZE];
	uint8_t next_sif[GF_PS2_SIF1_REARM_STATE_SIZE];
	gf_ps2_sif1_private_state next_private;
	uint32_t madr, stadr, qwc, sbus;
	uint16_t bcr_low;
	int stalled_early_return;

	if (iop_hw == NULL || ee_hw == NULL || sif1_state == NULL ||
		private_in == NULL || private_out == NULL ||
		iop_hw_size != GF_PS2_SIF1_REARM_HW_SIZE ||
		ee_hw_size != GF_PS2_SIF1_REARM_HW_SIZE ||
		sif1_state_size != GF_PS2_SIF1_REARM_STATE_SIZE ||
		!disjoint_inputs_and_outputs(iop_hw, ee_hw, sif1_state, private_in, private_out))
		return GF_PS2_SIF1_REARM_REFUSED;

	memcpy(&next_private, private_in, sizeof(next_private));
	if (next_private.dma_stall > 1 || next_private.reserved[0] != 0 ||
		next_private.reserved[1] != 0 || next_private.reserved[2] != 0)
		return GF_PS2_SIF1_REARM_REFUSED;

	/* Require the exact selected IOP start boundary after caller guest stores. */
	bcr_low = load16(iop_hw + IOP_DMA10_BCR);
	if (load32(iop_hw + IOP_DMA10_CHCR) != 0x41000300u || bcr_low != 0x0020u ||
		(load32(iop_hw + IOP_DMA_PCR2) & 0x00008000u) == 0)
		return GF_PS2_SIF1_REARM_REFUSED;

	/* This transition supports the selected empty-FIFO, EE-idle, qwc-zero route. */
	if (load32(sif1_state + SIF_FIFO_SIZE) != 0 || sif1_state[SIF_EE_BUSY] != 0)
		return GF_PS2_SIF1_REARM_REFUSED;
	if (sif1_state[SIF_IOP_END] > 1 || sif1_state[SIF_IOP_BUSY] > 1)
		return GF_PS2_SIF1_REARM_REFUSED;

	qwc = load32(ee_hw + EE_SIF1_QWC);
	if (qwc != 0)
		return GF_PS2_SIF1_REARM_REFUSED;

	memcpy(next_iop_hw, iop_hw, sizeof(next_iop_hw));
	memcpy(next_ee_hw, ee_hw, sizeof(next_ee_hw));
	memcpy(next_sif, sif1_state, sizeof(next_sif));

	/* IopDma.cpp::psxDma10 effects precede SIF1Dma's stall guard. */
	next_sif[SIF_IOP_BUSY] = 1;
	next_sif[SIF_IOP_END] = 0;

	madr = load32(ee_hw + EE_SIF1_MADR);
	stadr = load32(ee_hw + EE_DMAC_STADR);
	stalled_early_return = next_private.dma_stall && madr > stadr;
	if (stalled_early_return) {
		/* SIF1Dma returns before Sif1Init, clearing the private stall or Sif1End. */
		memcpy(sif1_state, next_sif, sizeof(next_sif));
		*private_out = next_private;
		return GF_PS2_SIF1_REARM_COMPLETE;
	}

	/* No transfer handler runs: FIFO is empty, EE channel idle, and IOP end is clear. */
	next_private.dma_stall = 0;
	store32(next_sif + SIF_EE_CYCLES, 0);
	store32(next_sif + SIF_IOP_CYCLES, 0);
	sbus = load32(next_ee_hw + EE_SBUS_F240);
	store32(next_ee_hw + EE_SBUS_F240, sbus & ~0x00004040u);

	memcpy(iop_hw, next_iop_hw, sizeof(next_iop_hw));
	memcpy(ee_hw, next_ee_hw, sizeof(next_ee_hw));
	memcpy(sif1_state, next_sif, sizeof(next_sif));
	*private_out = next_private;
	return GF_PS2_SIF1_REARM_COMPLETE;
}

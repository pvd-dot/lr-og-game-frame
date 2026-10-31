/*
 * Bounded PS2 SIF1 EE-to-IOP RAM data copy.
 *
 * This helper models only bytes carried by a prepared EE REF/REFE stream and
 * parsed IOP SIF headers. A successful result means DATA_COPIED only. It does
 * not mean that a syscall, device, interrupt, or receiver completed.
 *
 * Caller preconditions: the buffers are stable for the duration of the call,
 * describe base-zero physical RAM, and the EE SIF1 channel has TTE disabled.
 * See docs/consoles/ps2-functions.md for the support boundary.
 */
#ifndef GAMEFRAME_PS2_SIF1_DATA_H
#define GAMEFRAME_PS2_SIF1_DATA_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define GF_PS2_EE_RAM_SIZE  0x02000000u
#define GF_PS2_IOP_RAM_SIZE 0x00200000u
#define GF_PS2_SIF1_MAX_TAGS 4096u
#define GF_PS2_SIF1_MAX_PACKETS 65536u

typedef enum gf_ps2_sif1_status {
	GF_PS2_SIF1_INVALID = 0,
	GF_PS2_SIF1_DATA_COPIED = 1
} gf_ps2_sif1_status;

typedef struct gf_ps2_sif1_result {
	uint32_t tags_read;
	uint32_t packets_copied;
	uint32_t payload_bytes_copied;
	uint32_t final_stop_requested;
} gf_ps2_sif1_result;

/*
 * Consume a finite stream of sequential EE REF(ID 3) tags ending in REFE(ID
 * 0). Each tag's aligned source contributes QWC*16 bytes to one byte stream.
 * The stream consists of 16-byte IOP headers followed by their declared
 * payloads. Header data selects the low-24-bit IOP destination; header words
 * is masked with 0xffffc and counts 32-bit words.
 *
 * `first_tag_addr` is a base-zero physical EE address. `max_tags` bounds the
 * number of chain tags read and must not exceed GF_PS2_SIF1_MAX_TAGS. The
 * result is written only on success and must be disjoint from both RAM buffers.
 * The EE and IOP host buffers must be disjoint and exactly the documented RAM
 * sizes. Malformed/unsupported input returns INVALID without changing either
 * RAM buffer or result. Final header stop bits are accepted only when that
 * packet consumes the final stream bytes.
 */
gf_ps2_sif1_status gf_ps2_sif1_copy_data(
	uint8_t *ee_ram, size_t ee_ram_size,
	uint8_t *iop_ram, size_t iop_ram_size,
	uint32_t first_tag_addr, size_t max_tags,
	gf_ps2_sif1_result *result);

#ifdef __cplusplus
}
#endif

#endif

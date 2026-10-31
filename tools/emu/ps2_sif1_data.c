/* See ps2_sif1_data.h for the deliberately narrow data-plane contract. */
#include "ps2_sif1_data.h"

#include <stdalign.h>
#include <stdlib.h>
#include <string.h>

typedef struct sif1_segment {
	uint32_t source;
	size_t size;
} sif1_segment;

typedef struct sif1_write {
	uint32_t destination;
	size_t stream_offset;
	size_t size;
} sif1_write;

static uint32_t read_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static int span_valid(size_t total, uint32_t address, size_t size)
{
	return (size_t)address <= total && size <= total - (size_t)address;
}

static int host_span(const void *p, size_t size, uintptr_t *start, uintptr_t *end)
{
	uintptr_t first;

	if (p == NULL)
		return 0;
	first = (uintptr_t)p;
	if (size > UINTPTR_MAX - first)
		return 0;
	*start = first;
	*end = first + size;
	return 1;
}

static int spans_overlap(uintptr_t a0, uintptr_t a1, uintptr_t b0, uintptr_t b1)
{
	return a0 < b1 && b0 < a1;
}

static int read_stream_header(const uint8_t *stream, size_t stream_size,
			      size_t offset, uint32_t *data, uint32_t *words,
			      uint32_t *stop_bits)
{
	if (offset > stream_size || stream_size - offset < 16u)
		return 0;
	*data = read_le32(stream + offset);
	*words = read_le32(stream + offset + 4u);
	*stop_bits = *data & 0xc0000000u;
	return 1;
}

gf_ps2_sif1_status gf_ps2_sif1_copy_data(
	uint8_t *ee_ram, size_t ee_ram_size,
	uint8_t *iop_ram, size_t iop_ram_size,
	uint32_t first_tag_addr, size_t max_tags,
	gf_ps2_sif1_result *result)
{
	sif1_segment *segments = NULL;
	sif1_write *writes = NULL;
	uint8_t *stream = NULL;
	uintptr_t ee0, ee1, iop0, iop1, result0, result1;
	size_t segment_count = 0, stream_size = 0, packet_count = 0;
	size_t payload_total = 0, cursor = 0, i;
	int ended = 0, final_stop = 0;
	uint32_t tag_addr;
	gf_ps2_sif1_result completed;
	gf_ps2_sif1_status status = GF_PS2_SIF1_INVALID;

	if (ee_ram_size != GF_PS2_EE_RAM_SIZE || iop_ram_size != GF_PS2_IOP_RAM_SIZE ||
	    max_tags == 0 || max_tags > GF_PS2_SIF1_MAX_TAGS ||
	    first_tag_addr % 16u != 0 || result == NULL ||
	    (uintptr_t)result % alignof(gf_ps2_sif1_result) != 0 ||
	    !host_span(ee_ram, ee_ram_size, &ee0, &ee1) ||
	    !host_span(iop_ram, iop_ram_size, &iop0, &iop1) ||
	    !host_span(result, sizeof(*result), &result0, &result1) ||
	    spans_overlap(ee0, ee1, iop0, iop1) ||
	    spans_overlap(result0, result1, ee0, ee1) ||
	    spans_overlap(result0, result1, iop0, iop1))
		return GF_PS2_SIF1_INVALID;

	segments = (sif1_segment *)calloc(max_tags, sizeof(*segments));
	writes = (sif1_write *)calloc(GF_PS2_SIF1_MAX_PACKETS, sizeof(*writes));
	if (segments == NULL || writes == NULL)
		goto done;

	/* First validate the entire sequential REF/REFE descriptor chain. */
	tag_addr = first_tag_addr;
	for (i = 0; i < max_tags; ++i) {
		uint32_t tag_word0, source, id;
		size_t qwc, bytes;

		if (!span_valid(ee_ram_size, tag_addr, 16u))
			goto done;
		tag_word0 = read_le32(ee_ram + tag_addr);
		id = (tag_word0 >> 28) & 7u;
		qwc = (size_t)(tag_word0 & 0xffffu);
		/* Reject IRQ and reserved tag bits. TTE must be disabled by caller. */
		if ((tag_word0 & 0x8fff0000u) != 0 || (id != 0u && id != 3u))
			goto done;
		source = read_le32(ee_ram + tag_addr + 4u);
		if ((source & 15u) != 0 || qwc > SIZE_MAX / 16u)
			goto done;
		bytes = qwc * 16u;
		if (!span_valid(ee_ram_size, source, bytes) ||
		    bytes > GF_PS2_EE_RAM_SIZE - stream_size)
			goto done;

		segments[segment_count].source = source;
		segments[segment_count].size = bytes;
		++segment_count;
		stream_size += bytes;

		if (id == 0u) {
			ended = 1;
			break;
		}
		if (tag_addr > UINT32_MAX - 16u)
			goto done;
		tag_addr += 16u;
	}
	if (!ended || stream_size < 16u)
		goto done;

	stream = (uint8_t *)malloc(stream_size);
	if (stream == NULL)
		goto done;
	for (i = 0; i < segment_count; ++i) {
		if (segments[i].size != 0)
			memcpy(stream + cursor, ee_ram + segments[i].source, segments[i].size);
		cursor += segments[i].size;
	}

	/* Parse every header and validate every destination before the first write. */
	cursor = 0;
	while (cursor < stream_size) {
		uint32_t data, words, stop_bits;
		size_t payload_bytes;
		uint32_t destination;

		if (packet_count == GF_PS2_SIF1_MAX_PACKETS ||
		    !read_stream_header(stream, stream_size, cursor, &data, &words, &stop_bits))
			goto done;
		if ((words & 3u) != 0)
			goto done;
		payload_bytes = (size_t)(words & 0x000ffffcu) * 4u;
		destination = data & 0x00ffffffu;
		if ((destination & 3u) != 0 ||
		    !span_valid(iop_ram_size, destination, payload_bytes) ||
		    payload_bytes > stream_size - cursor - 16u)
			goto done;

		writes[packet_count].destination = destination;
		writes[packet_count].stream_offset = cursor + 16u;
		writes[packet_count].size = payload_bytes;
		++packet_count;
		payload_total += payload_bytes;
		cursor += 16u + payload_bytes;
		if (stop_bits != 0) {
			if (cursor != stream_size)
				goto done;
			final_stop = 1;
		}
	}
	if (packet_count == 0 || cursor != stream_size)
		goto done;

	/* All fallible validation/allocation is complete; these copies cannot fail. */
	for (i = 0; i < packet_count; ++i) {
		if (writes[i].size != 0)
			memcpy(iop_ram + writes[i].destination,
			       stream + writes[i].stream_offset, writes[i].size);
	}
	completed.tags_read = (uint32_t)segment_count;
	completed.packets_copied = (uint32_t)packet_count;
	completed.payload_bytes_copied = (uint32_t)payload_total;
	completed.final_stop_requested = (uint32_t)final_stop;
	*result = completed;
	status = GF_PS2_SIF1_DATA_COPIED;

done:
	free(stream);
	free(writes);
	free(segments);
	return status;
}

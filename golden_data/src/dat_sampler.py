"""Read selected ibaPDA channels directly at a low sampling frequency.

Unlike :mod:`parse_ibapda`, this decoder does not expand every 100 Hz RLE run
to a Python list.  It picks the requested 1 Hz samples while walking the RLE
chain, which makes a full-history audit practical and leaves the raw DAT files
unchanged.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from struct import unpack
from typing import Any, Iterable


def decadr(value: int) -> int:
    b0, b1, b2, b3, b4, b5, b6, b7 = value.to_bytes(8, "little")
    a0, a1, a2 = b6 ^ b0, b5 ^ b4 ^ b3, b7 ^ b1
    return a0 | a1 << 8 | a2 << 16


def _read_exact(handle, size: int, context: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError(f"short read for {context}: {len(data)}/{size}")
    return data


def _decode_name(raw: bytes) -> str:
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return raw.decode("latin1", errors="replace")


def _sample_channel(
    handle,
    *,
    start_offset: int,
    digital: bool,
    frames: int,
    sample_indices: list[int],
    file_size: int,
    minimum_offset: int,
    channel_name: str,
) -> list[Any]:
    result: list[Any] = []
    wanted = 0
    frame_position = 0
    next_offset = start_offset
    seen: set[int] = set()
    record_size = 2 if digital else 5

    while next_offset and wanted < len(sample_indices):
        if next_offset in seen:
            raise ValueError(f"cyclic RLE chain for {channel_name}")
        if next_offset < minimum_offset or next_offset + 6 > file_size:
            raise ValueError(f"invalid channel offset for {channel_name}")
        seen.add(next_offset)
        handle.seek(next_offset)
        count_records = int.from_bytes(_read_exact(handle, 2, "record count"), "little")
        chained_offset = int.from_bytes(_read_exact(handle, 4, "next offset"), "little")
        payload = _read_exact(handle, count_records * record_size, "RLE payload")
        for record_index in range(count_records):
            pos = record_index * record_size
            if digital:
                packed = int.from_bytes(payload[pos : pos + 2], "little")
                run_length = packed & 0x3FFF
                value: Any = bool(packed & 0x8000)
            else:
                run_length = payload[pos]
                value = unpack("<f", payload[pos + 1 : pos + 5])[0]
                if not math.isfinite(value):
                    value = None
            if run_length <= 0:
                raise ValueError(f"zero RLE run for {channel_name}")
            run_end = frame_position + run_length
            while wanted < len(sample_indices) and sample_indices[wanted] < run_end:
                if sample_indices[wanted] >= frame_position:
                    result.append(value)
                    wanted += 1
                else:
                    wanted += 1
            frame_position = run_end
        next_offset = chained_offset

    if wanted != len(sample_indices):
        raise ValueError(
            f"sample count mismatch for {channel_name}: {wanted}/{len(sample_indices)}"
        )
    return result


def sample_dat(
    path: str | Path,
    channels: Iterable[str],
    sample_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Return exact named channels sampled at approximately the requested rate."""
    path = Path(path)
    requested = set(channels)
    with path.open("rb") as handle:
        file_size = os.fstat(handle.fileno()).st_size
        header = _read_exact(handle, 12, "file header")
        if header[:4] != b"PDA2":
            raise ValueError(f"not ibaPDA: {path}")
        ascii_offset = int.from_bytes(header[8:12], "little")
        handle.seek(ascii_offset)
        # Metadata is at the beginning of the ASCII section.  Bound the read so
        # a 10 MiB process file is not copied in full merely to locate channels.
        blob = handle.read(2 * 1024 * 1024)
        start_raw = blob.split(b"\r\nstarttime:")[1].split(b"\r\n")[0].decode()
        clk = float(blob.split(b"\r\nclk:")[1].split()[0])
        frames = int(blob.split(b"\r\nframes:")[1].split(b"\r\n")[0])
        start_dt = datetime.strptime(start_raw.strip(), "%d.%m.%Y %H:%M:%S.%f")
        metadata: dict[str, tuple[int, bool]] = {}
        for block in blob.split(b"\r\nname:")[1:]:
            name = _decode_name(block.split(b"\r\n")[0])
            if name not in requested:
                continue
            digital = b"\r\ndigchannel:" in block
            encoded = block.split(b"\r\nchannel_offset:O")[1].split()[0]
            metadata[name] = (decadr(int(encoded, 16)), digital)

        step = max(1, int(round(sample_interval_seconds / clk)))
        sample_indices = list(range(0, frames, step))
        sampled: dict[str, list[Any]] = {}
        errors: dict[str, str] = {}
        for name in requested:
            if name not in metadata:
                errors[name] = "missing channel"
                continue
            offset, digital = metadata[name]
            try:
                sampled[name] = _sample_channel(
                    handle,
                    start_offset=offset,
                    digital=digital,
                    frames=frames,
                    sample_indices=sample_indices,
                    file_size=file_size,
                    minimum_offset=ascii_offset,
                    channel_name=name,
                )
            except Exception as exc:  # one broken channel must not lose the file
                errors[name] = f"{type(exc).__name__}: {exc}"

    timestamps = [start_dt + timedelta(seconds=index * clk) for index in sample_indices]
    return {
        "filename": path.name,
        "start_dt": start_dt,
        "clk": clk,
        "frames": frames,
        "timestamps": timestamps,
        "data": sampled,
        "errors": errors,
    }

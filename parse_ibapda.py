#!/usr/bin/env python3
"""
ibaPDA .dat 文件解析器
=====================
解析永通轧机 ibaPDA v6.24 采集的 .dat 数据文件。
支持从 RAR 压缩包批量提取并转换为 CSV/JSON。

格式说明:
- 文件头: PDA2 magic + 偏移量指向 ASCII 元数据区
- ASCII 区: 包含 header (时间/采样率/帧数) + channel 定义 (名称/地址)
- 二进制区: RLE 压缩的 float32 时间序列 (每通道独立链表)
- 地址加密: channel_offset 使用 XOR 混淆，需 decadr() 解密

用法:
    python3 parse_ibapda.py <input.dat|input.rar> [--channels CH1,CH2,...] [--csv] [--json]
    python3 parse_ibapda.py 260703.rar --csv --channels "YH.PLC.YH.1#厚差mm,YH.PLC.YH.1#卷取机速度"
    python3 parse_ibapda.py pda750.dat --json
    python3 parse_ibapda.py 260703.rar --list-channels
"""

import os
import sys
import json
import csv
import subprocess
import tempfile
from struct import error as StructError, unpack
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


def decadr(b: int) -> int:
    """解密 ibaPDA channel_offset 地址 (XOR 混淆)"""
    b0, b1, b2, b3, b4, b5, b6, b7 = b.to_bytes(8, 'little')
    a0, a1, a2 = b6 ^ b0, b5 ^ b4 ^ b3, b7 ^ b1
    return a0 | a1 << 8 | a2 << 16


def _read_exact(f, size: int, context: str) -> bytes:
    """读取固定字节数；短读时给出包含上下文的解析错误。"""
    data = f.read(size)
    if len(data) != size:
        raise ValueError(
            f"Truncated ibaPDA data while reading {context}: "
            f"expected {size} bytes, got {len(data)}"
        )
    return data


def _validate_data_offset(offset: int, file_size: int, minimum_offset: int,
                          context: str) -> None:
    """验证 RLE 数据块偏移至少能容纳 6 字节块头。"""
    if offset < minimum_offset or offset + 6 > file_size:
        raise ValueError(
            f"Invalid ibaPDA data offset {offset} for {context} "
            f"(file size={file_size})"
        )


def _decode_channel_data(f, start_offset: int, digital: bool, frames: int,
                         file_size: int, minimum_offset: int,
                         channel_name: str) -> List[object]:
    """解码一个通道的 RLE 链表，并验证链表与最终样本数。"""
    nxt = start_offset
    vals = []
    seen_offsets = set()
    record_size = 2 if digital else 5

    while nxt:
        _validate_data_offset(nxt, file_size, minimum_offset, channel_name)
        if nxt in seen_offsets:
            raise ValueError(
                f"Cyclic ibaPDA RLE chain for channel {channel_name!r} "
                f"at offset {nxt}"
            )
        seen_offsets.add(nxt)

        f.seek(nxt)
        n = int.from_bytes(_read_exact(f, 2, f"{channel_name} block count"),
                           'little')
        next_offset = int.from_bytes(
            _read_exact(f, 4, f"{channel_name} next offset"), 'little'
        )
        payload_size = n * record_size
        if f.tell() + payload_size > file_size:
            raise ValueError(
                f"Truncated ibaPDA RLE payload for channel {channel_name!r} "
                f"at offset {nxt}: need {payload_size} bytes"
            )
        payload = _read_exact(f, payload_size, f"{channel_name} RLE payload")

        for record_index in range(n):
            pos = record_index * record_size
            if digital:
                # 数字量记录是一个 little-endian uint16：低 14 位为
                # 重复次数，最高位为布尔值，bit 14 是格式标志位。
                packed = int.from_bytes(payload[pos:pos + 2], 'little')
                count = packed & 0x3FFF
                value = bool(packed & 0x8000)
            else:
                count = payload[pos]
                value = unpack('<f', payload[pos + 1:pos + 5])[0]

            if count == 0:
                raise ValueError(
                    f"Zero-length ibaPDA RLE run for channel {channel_name!r} "
                    f"at offset {nxt}, record {record_index}"
                )
            if len(vals) + count > frames:
                raise ValueError(
                    f"Too many samples for channel {channel_name!r}: "
                    f"decoded more than declared {frames} frames"
                )
            vals.extend([value] * count)

        nxt = next_offset

    if len(vals) != frames:
        raise ValueError(
            f"Sample count mismatch for channel {channel_name!r}: "
            f"decoded {len(vals)}, expected {frames}"
        )
    return vals


def parse_dat(
    filepath: str,
    channel_filter: Optional[List[str]] = None,
    *,
    exact_channel_filter: Optional[List[str]] = None,
    skip_channel_errors: bool = False,
) -> dict:
    """
    解析单个 ibaPDA .dat 文件

    Args:
        filepath: .dat 文件路径
        channel_filter: 可选，只解析指定通道名（支持部分匹配）
        exact_channel_filter: 可选，只解析名称完全一致的通道。用于 ``#3``、
            ``#30`` 这类前缀相同的通道；与 ``channel_filter`` 不能同时使用。
        skip_channel_errors: 单个通道损坏时是否继续解析其他通道。默认仍严格
            报错；批量特征提取可设为 True，并从 ``channel_errors`` 检查缺失。

    Returns:
        dict: {
            'filename': str,
            'starttime': str,
            'start_dt': datetime,
            'clk': float (秒),
            'frames': int,
            'channels': list[str],  # 所有通道名
            'data': dict[str, list[float|bool]]  # 通道名 -> 数据数组
        }
    """
    with open(filepath, 'rb') as f:
        file_size = os.fstat(f.fileno()).st_size

        # 读取文件头
        header = _read_exact(f, 12, f"{filepath} file header")
        magic = header[:4]
        if magic != b'PDA2':
            raise ValueError(f"Not an ibaPDA file: {filepath} (magic={magic})")
        ascii_offset = int.from_bytes(header[8:12], 'little')
        if ascii_offset < 12 or ascii_offset >= file_size:
            raise ValueError(
                f"Invalid ibaPDA metadata offset {ascii_offset} for {filepath} "
                f"(file size={file_size})"
            )

        # 读取 ASCII 元数据区
        f.seek(ascii_offset)
        ascii_data = f.read()

        # 解析 header
        starttime = ascii_data.split(b'\r\nstarttime:')[1].split(b'\r\n')[0].decode()
        clk = float(ascii_data.split(b'\r\nclk:')[1].split()[0])
        frames = int(ascii_data.split(b'\r\nframes:')[1].split(b'\r\n')[0])

        # 解析时间 (格式: "DD.MM.YYYY HH:MM:SS.ffffff")
        try:
            # 分离日期时间和微秒
            parts = starttime.strip().split(' ')
            date_part = parts[0]  # DD.MM.YYYY
            time_parts = parts[1].split('.')  # HH:MM:SS 和 ffffff
            time_hms = time_parts[0]  # HH:MM:SS
            microseconds = int(time_parts[1]) if len(time_parts) > 1 else 0
            start_dt = datetime.strptime(f"{date_part} {time_hms}", '%d.%m.%Y %H:%M:%S')
            start_dt = start_dt.replace(microsecond=microseconds)
        except (ValueError, IndexError):
            start_dt = None

        # 解析通道元数据
        channels = []
        for block in ascii_data.split(b'\r\nname:')[1:]:
            name_raw = block.split(b'\r\n')[0]
            try:
                name_str = name_raw.decode('gbk')
            except (UnicodeDecodeError, LookupError):
                name_str = name_raw.decode('latin1')
            dig = b'\r\ndigchannel:' in block
            offset_hex = block.split(b'\r\nchannel_offset:O')[1].split()[0]
            nxt = decadr(int(offset_hex, 16))
            if nxt:
                _validate_data_offset(nxt, file_size, ascii_offset, name_str)
            channels.append({'name': name_str, 'offset': nxt, 'digital': dig})

        all_channel_names = [ch['name'] for ch in channels]

        if channel_filter and exact_channel_filter:
            raise ValueError(
                "channel_filter and exact_channel_filter cannot be used together"
            )

        # 确定要读取的通道。精确匹配必须先于原有的兼容性部分匹配，避免
        # 请求“#3”时误把“#30”解码进来。
        if exact_channel_filter:
            exact_names = set(exact_channel_filter)
            target_channels = [ch for ch in channels if ch['name'] in exact_names]
        elif channel_filter:
            target_channels = []
            for ch in channels:
                for flt in channel_filter:
                    if flt in ch['name'] or ch['name'] in flt:
                        target_channels.append(ch)
                        break
        else:
            target_channels = channels

        requested_filter = exact_channel_filter or channel_filter
        if requested_filter and not target_channels:
            raise ValueError(
                f"No channels matched filter {requested_filter!r} in {filepath}"
            )

        # 读取通道数据 (RLE 解码)
        channel_data = {}
        channel_errors = {}
        for ch in target_channels:
            try:
                channel_data[ch['name']] = _decode_channel_data(
                    f=f,
                    start_offset=ch['offset'],
                    digital=ch['digital'],
                    frames=frames,
                    file_size=file_size,
                    minimum_offset=ascii_offset,
                    channel_name=ch['name'],
                )
            except (OSError, ValueError, StructError) as exc:
                if not skip_channel_errors:
                    raise
                channel_errors[ch['name']] = str(exc)

        return {
            'filename': os.path.basename(filepath),
            'starttime': starttime,
            'start_dt': start_dt,
            'clk': clk,
            'frames': frames,
            'channels': all_channel_names,
            'data': channel_data,
            'channel_errors': channel_errors,
        }


def extract_rar(rar_path: str, output_dir: str) -> List[str]:
    """用 bsdtar 解压 RAR，返回 .dat 文件路径列表"""
    subprocess.run(['/usr/bin/bsdtar', '-xf', rar_path, '-C', output_dir],
                   check=True, capture_output=True)
    dat_files = []
    for root, dirs, files in os.walk(output_dir):
        for fname in sorted(files):
            if fname.endswith('.dat'):
                dat_files.append(os.path.join(root, fname))
    return sorted(dat_files)


def _write_csv_result(writer, result: dict, all_channels: List[str]) -> None:
    """把单个解析结果逐行写入已打开的 CSV。"""
    start_dt = result['start_dt']
    clk = result['clk']
    frames = result['frames']

    for i in range(frames):
        if start_dt:
            ts = start_dt + timedelta(seconds=i * clk)
            ts_str = ts.strftime('%Y-%m-%d %H:%M:%S.') + f'{ts.microsecond // 10000:02d}'
        else:
            ts_str = f"{result['starttime']}+{i*clk:.2f}s"

        row = [ts_str]
        for ch in all_channels:
            data = result['data'].get(ch, [])
            row.append(data[i] if i < len(data) else '')
        writer.writerow(row)


def export_csv(results: List[dict], output_path: str):
    """导出为 CSV (时间戳 + 各通道值)。CLI 批量路径不使用此接口。"""
    if not results:
        return

    all_channels = list(results[0]['data'].keys())

    with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp'] + all_channels)
        for result in results:
            _write_csv_result(writer, result, all_channels)

    print(f"CSV exported: {output_path} ({os.path.getsize(output_path) / 1024 / 1024:.1f} MB)")


def _json_entry(result: dict) -> dict:
    """构造单文件 JSON 摘要，不保留完整采样数组。"""
    entry = {
        'filename': result['filename'],
        'starttime': result['starttime'],
        'clk': result['clk'],
        'frames': result['frames'],
        'channels': {}
    }
    for ch_name, vals in result['data'].items():
        entry['channels'][ch_name] = {
            'samples': len(vals),
            'min': min(vals) if vals and not isinstance(vals[0], bool) else None,
            'max': max(vals) if vals and not isinstance(vals[0], bool) else None,
            'preview': vals[:100]
        }
    return entry


def export_json(results: List[dict], output_path: str):
    """导出为 JSON。CLI 批量路径按文件流式写入。"""
    output = [_json_entry(result) for result in results]

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"JSON exported: {output_path}")


def list_channels(filepath: str):
    """列出文件中所有通道名"""
    with open(filepath, 'rb') as f:
        f.read(8)
        ascii_offset = int.from_bytes(f.read(4), 'little')
        f.seek(ascii_offset)
        ascii_data = f.read()

    starttime = ascii_data.split(b'\r\nstarttime:')[1].split(b'\r\n')[0].decode()
    clk = float(ascii_data.split(b'\r\nclk:')[1].split()[0])
    frames = int(ascii_data.split(b'\r\nframes:')[1].split(b'\r\n')[0])

    channels = []
    for block in ascii_data.split(b'\r\nname:')[1:]:
        name_raw = block.split(b'\r\n')[0]
        try:
            name_str = name_raw.decode('gbk')
        except:
            name_str = name_raw.decode('latin1')
        channels.append(name_str)

    print(f"文件: {filepath}")
    print(f"时间: {starttime}")
    print(f"采样: {clk}s ({1/clk:.0f} Hz), {frames} 帧 ({frames*clk:.0f}s)")
    print(f"通道数: {len(channels)}")
    print(f"{'='*60}")

    # 按设备分组
    groups = {}
    for ch in channels:
        if '#' in ch:
            prefix = ch.split('#')[0]
        elif ch.count('-') >= 2:
            parts = ch.rsplit('-', 1)
            prefix = parts[0]
        else:
            prefix = ch
        groups.setdefault(prefix, []).append(ch)

    for prefix, chs in sorted(groups.items()):
        print(f"\n[{prefix}] ({len(chs)} channels)")
        for ch in chs:
            print(f"  {ch}")


def _process_dat_files(dat_files, args, input_path: str,
                       channel_filter: Optional[List[str]]) -> int:
    """逐文件执行 CLI 解析和输出，返回进程状态码。"""
    if args.max_files > 0:
        dat_files = dat_files[:args.max_files]

    if not dat_files:
        print("没有找到 .dat 文件", file=sys.stderr)
        return 1

    # 列出通道
    if args.list_channels:
        try:
            list_channels(dat_files[0])
            return 0
        except Exception as exc:
            print("失败清单:", file=sys.stderr)
            print(f"- {dat_files[0]}: {exc}", file=sys.stderr)
            return 1

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    csv_path = (args.output or f"{base_name}_data.csv") if args.csv else None
    json_path = (args.output or f"{base_name}_data.json") if args.json else None
    csv_file = None
    csv_writer = None
    csv_channels = None
    json_file = None
    json_first = True

    success_count = 0
    failures = []
    first_starttime = None
    last_starttime = None
    first_clk = None
    first_frames = None
    first_channel_count = None
    first_selected_channels = None

    try:
        if csv_path:
            csv_file = open(csv_path, 'w', newline='', encoding='utf-8-sig')
            csv_writer = csv.writer(csv_file)
        if json_path:
            json_file = open(json_path, 'w', encoding='utf-8')
            json_file.write('[\n')

        for i, dat_file in enumerate(dat_files):
            print(f"[{i+1}/{len(dat_files)}] 解析 {os.path.basename(dat_file)}...", end=' ')
            result = None
            try:
                result = parse_dat(dat_file, channel_filter)

                if success_count == 0:
                    first_starttime = result['starttime']
                    first_clk = result['clk']
                    first_frames = result['frames']
                    first_channel_count = len(result['channels'])
                    first_selected_channels = list(result['data'].keys())
                last_starttime = result['starttime']

                if csv_writer is not None:
                    if csv_channels is None:
                        csv_channels = list(result['data'].keys())
                        csv_writer.writerow(['timestamp'] + csv_channels)
                    _write_csv_result(csv_writer, result, csv_channels)

                if json_file is not None:
                    if not json_first:
                        json_file.write(',\n')
                    entry = _json_entry(result)
                    json.dump(entry, json_file, ensure_ascii=False,
                              indent=2, default=str)
                    del entry
                    json_first = False

                success_count += 1
                print(f"OK ({result['starttime']}, {len(result['data'])} channels)")
            except Exception as exc:
                failures.append((dat_file, str(exc)))
                print(f"ERROR: {exc}")
            finally:
                # parse_dat 会展开一个文件的筛选通道；显式释放，确保开始
                # 下一个文件前不保留上一文件的完整采样数组。
                result = None
    finally:
        if json_file is not None:
            json_file.write('\n]\n')
            json_file.close()
        if csv_file is not None:
            csv_file.close()

    if csv_path and success_count:
        print(f"CSV exported: {csv_path} ({os.path.getsize(csv_path) / 1024 / 1024:.1f} MB)")
    if json_path:
        print(f"JSON exported: {json_path}")

    if success_count == 0:
        print("没有成功解析的文件", file=sys.stderr)
    elif not args.csv and not args.json:
        print(f"\n{'='*60}")
        print(f"解析完成: {success_count} 个文件")
        print(f"时间范围: {first_starttime} ~ {last_starttime}")
        print(f"采样率: {first_clk}s ({1/first_clk:.0f} Hz)")
        print(f"每文件帧数: {first_frames} ({first_frames * first_clk:.0f}s)")
        print(f"通道数: {first_channel_count}")
        if channel_filter:
            print(f"已筛选通道: {first_selected_channels}")

    if failures:
        print("\n失败清单:", file=sys.stderr)
        for dat_file, error in failures:
            print(f"- {dat_file}: {error}", file=sys.stderr)
        return 1
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description='ibaPDA .dat 文件解析器 (永通轧机)')
    parser.add_argument('input', help='.dat 文件或 .rar 压缩包路径')
    parser.add_argument('--channels', '-c', help='指定通道名(逗号分隔，支持部分匹配)')
    parser.add_argument('--csv', action='store_true', help='导出为 CSV')
    parser.add_argument('--json', action='store_true', help='导出为 JSON')
    parser.add_argument('--list-channels', '-l', action='store_true', help='列出所有通道')
    parser.add_argument('--output', '-o', help='输出文件路径')
    parser.add_argument('--max-files', type=int, default=0, help='最多处理文件数(0=全部)')

    args = parser.parse_args()

    if args.csv and args.json and args.output:
        parser.error('--csv 和 --json 同时使用时不能共用一个 --output 路径')

    input_path = args.input
    channel_filter = args.channels.split(',') if args.channels else None

    # 处理 RAR 或单个 .dat。TemporaryDirectory 确保正常结束、提前
    # return 或抛异常时都能清理解压数据。
    if input_path.lower().endswith('.rar'):
        with tempfile.TemporaryDirectory(prefix='ibapda_') as tmpdir:
            print(f"解压 RAR 到: {tmpdir}")
            dat_files = extract_rar(input_path, tmpdir)
            print(f"找到 {len(dat_files)} 个 .dat 文件")
            status = _process_dat_files(dat_files, args, input_path, channel_filter)
    elif input_path.lower().endswith('.dat'):
        status = _process_dat_files([input_path], args, input_path, channel_filter)
    else:
        print(f"不支持的文件格式: {input_path}")
        sys.exit(1)

    if status:
        sys.exit(status)


if __name__ == '__main__':
    main()

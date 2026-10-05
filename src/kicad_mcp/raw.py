"""Read one real-valued SPICE RAW plot without guessing incompatible layouts."""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
import csv
import math
import re
import struct


class RawFormatError(ValueError):
    """Malformed RAW file or an explicitly unsupported representation."""


@dataclass
class RawData:
    header: str
    variables: tuple[str, ...]
    vectors: dict[str, array]
    flags: tuple[str, ...]
    plot_name: str
    storage: str
    point_count: int

    @property
    def axis(self) -> array:
        return self.vectors[self.variables[0]]


def _header(path: Path) -> tuple[str, str, int, str]:
    # Bound header work. Binary payload is never decoded as text.
    with path.open('rb') as handle:
        prefix = handle.read(4 * 1024 * 1024)
    if prefix.startswith(b'\xff\xfe') or prefix.startswith(b'T\x00'):
        encoding = 'utf-16-le'
    elif prefix.startswith(b'\xfe\xff'):
        encoding = 'utf-16-be'
    else:
        encoding = 'utf-8'
    candidates = []
    for kind in ('Binary', 'Values'):
        for ending in ('\n', '\r\n'):
            marker = (kind + ':' + ending).encode(encoding)
            offset = prefix.find(marker)
            if offset >= 0 and (offset == 0 or prefix[:offset].endswith('\n'.encode(encoding))):
                candidates.append((offset, len(marker), kind))
    if not candidates:
        raise RawFormatError('RAW header has no Binary:/Values: marker within 4 MiB')
    offset, length, kind = min(candidates)
    try:
        text = prefix[:offset + length].decode(encoding).lstrip('\ufeff')
    except UnicodeDecodeError as exc:
        raise RawFormatError('Invalid RAW header encoding') from exc
    return text, encoding, offset + length, kind


def read_raw(path: str | Path, *, binary_format: str = 'auto') -> RawData:
    """Read a single real plot; complex, FastAccess, and concatenated plots fail.

    binary_format: auto, ltspice, or ngspice. ngspice binary is native-endian;
    this reader supports little-endian hosts/files only. auto recognizes ngspice
    in its header; otherwise it uses LTspice's documented flags and record size.
    """
    if binary_format not in ('auto', 'ltspice', 'ngspice'):
        raise ValueError('binary_format must be auto, ltspice, or ngspice')
    path = Path(path)
    header, encoding, offset, kind = _header(path)
    fields = {}
    for line in header.splitlines():
        if ':' in line:
            key, value = line.split(':', 1)
            fields[key.strip().lower()] = value.strip()
    flags = tuple(fields.get('flags', '').lower().split())
    if 'complex' in flags or 'fastaccess' in flags:
        raise RawFormatError('Complex and FastAccess RAW formats are not supported')
    if 'real' not in flags:
        raise RawFormatError('RAW must explicitly have the real flag')
    try:
        nvars = int(fields['no. variables'])
        npoints = int(fields['no. points'])
    except (KeyError, ValueError) as exc:
        raise RawFormatError('Missing or invalid variable/point count') from exc
    if not 1 <= nvars <= 100000 or not 1 <= npoints <= 1000000000:
        raise RawFormatError('Variable/point count is out of bounds')
    matches = re.findall(r'^[ \t]*(\d+)[ \t]+(\S+)[ \t]+(\S+)(?:[ \t]+.*)?[ \t\r]*$', header, re.M)
    if len(matches) != nvars or [int(m[0]) for m in matches] != list(range(nvars)):
        raise RawFormatError('Variable table does not match its declared count/order')
    names = tuple(m[1] for m in matches)
    if len({name.casefold() for name in names}) != nvars:
        raise RawFormatError('Duplicate RAW variable names')
    vectors = {name: array('d') for name in names}
    remaining = path.stat().st_size - offset
    if kind == 'Binary':
        source = binary_format
        if source == 'auto':
            source = 'ngspice' if re.search(r'ngspice', header, re.I) else 'ltspice'
        format_string = '<' + ('d' * nvars if source == 'ngspice' or 'double' in flags else 'd' + 'f' * (nvars - 1))
        record = struct.Struct(format_string)
        expected = npoints * record.size
        if remaining != expected:
            raise RawFormatError(f'Binary size mismatch: expected {expected}, got {remaining}; truncated, concatenated, or wrong layout')
        # Stream bounded blocks rather than holding the binary payload twice.
        rows_per_block = max(1, 1048576 // record.size)
        with path.open('rb') as handle:
            handle.seek(offset)
            for first in range(0, npoints, rows_per_block):
                count = min(rows_per_block, npoints - first)
                block = handle.read(record.size * count)
                if len(block) != record.size * count:
                    raise RawFormatError('RAW changed or was truncated during reading')
                for values in record.iter_unpack(block):
                    for name, value in zip(names, values):
                        vectors[name].append(value)
        storage = source + ('-double' if source == 'ngspice' or 'double' in flags else '-mixed')
    else:
        if remaining < npoints * (nvars + 1):
            raise RawFormatError('ASCII payload is shorter than its declared point count')
        with path.open('rb') as handle:
            handle.seek(offset)
            try:
                payload = handle.read().decode(encoding)
            except UnicodeDecodeError as exc:
                raise RawFormatError('Invalid ASCII RAW payload encoding') from exc
        tokens = payload.split()
        if len(tokens) != npoints * (nvars + 1):
            raise RawFormatError('ASCII value count mismatch; multiple plots are not supported')
        for row in range(npoints):
            index = row * (nvars + 1)
            if tokens[index] != str(row):
                raise RawFormatError(f'Unexpected ASCII point index at point {row}')
            try:
                for col, name in enumerate(names):
                    vectors[name].append(float(tokens[index + col + 1]))
            except ValueError as exc:
                raise RawFormatError(f'Invalid real value at point {row}') from exc
        storage = 'ascii'
    return RawData(header, names, vectors, flags, fields.get('plotname', ''), storage, npoints)


def _signals(raw: RawData, names: list[str] | None) -> list[str]:
    if names is None:
        return list(raw.variables[1:])
    lookup = {name.casefold(): name for name in raw.variables}
    chosen = []
    for name in names:
        try:
            canonical = lookup[name.casefold()]
        except KeyError as exc:
            raise ValueError(f'Unknown RAW signal: {name}; available: {", ".join(raw.variables)}') from exc
        if canonical in chosen:
            raise ValueError(f'Duplicate requested signal: {name}')
        chosen.append(canonical)
    return chosen


def _interpolate(axis: array, values: array, at: float) -> float:
    index = bisect_left(axis, at)
    if index < len(axis) and axis[index] == at:
        return values[index]
    lower, upper = index - 1, index
    weight = (at - axis[lower]) / (axis[upper] - axis[lower])
    return values[lower] * (1 - weight) + values[upper] * weight


def summarize_raw(path: str | Path, signals: list[str] | None = None,
                  start: float | None = None, end: float | None = None,
                  *, binary_format: str = 'auto') -> dict:
    """Statistics of saved points with interpolated window boundaries.

    Time mean is trapezoid-integrated with actual adaptive time steps. This is
    not a circuit pass/fail criterion and cannot see between unsaved points.
    """
    raw = read_raw(path, binary_format=binary_format)
    axis = raw.axis
    if not all(math.isfinite(value) for value in axis):
        raise RawFormatError('Non-finite independent axis')
    if any(b <= a for a, b in zip(axis, axis[1:])):
        raise RawFormatError('Axis must be strictly increasing; split stepped/repeated-time plots first')
    start = axis[0] if start is None else float(start)
    end = axis[-1] if end is None else float(end)
    if not math.isfinite(start) or not math.isfinite(end) or not axis[0] <= start <= end <= axis[-1]:
        raise ValueError(f'Requested interval [{start}, {end}] is outside [{axis[0]}, {axis[-1]}]')
    selected = _signals(raw, signals)
    first, last = bisect_right(axis, start), bisect_left(axis, end)
    x = [start] + list(axis[first:last]) + ([end] if end > start else [])
    summary = {}
    for name in selected:
        vector = raw.vectors[name]
        y = [_interpolate(axis, vector, start)] + list(vector[first:last]) + ([_interpolate(axis, vector, end)] if end > start else [])
        finite = all(math.isfinite(value) for value in y)
        weighted = None
        if finite and end > start:
            weighted = math.fsum((x[i + 1] - x[i]) * (y[i] + y[i + 1]) / 2 for i in range(len(x) - 1)) / (end - start)
        summary[name] = {'min': min(y) if finite else None, 'max': max(y) if finite else None,
                         'axis_weighted_mean': weighted, 'all_finite': finite}
    return {'plot_name': raw.plot_name, 'storage': raw.storage, 'point_count': raw.point_count,
            'axis': raw.variables[0], 'start': start, 'end': end, 'signals': summary,
            'all_finite': all(math.isfinite(v) for vector in raw.vectors.values() for v in vector),
            'scope': 'saved waveform points; linear boundary interpolation; no circuit acceptance criterion'}


def write_csv(raw_or_path: RawData | str | Path, path: str | Path,
              signals: list[str] | None = None, *, binary_format: str = 'auto') -> str:
    """Export native saved points without resampling. Never overwrite a file."""
    raw = raw_or_path if isinstance(raw_or_path, RawData) else read_raw(raw_or_path, binary_format=binary_format)
    chosen = [raw.variables[0]] + [name for name in _signals(raw, signals) if name != raw.variables[0]]
    path = Path(path).resolve()
    with path.open('x', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(chosen)
        writer.writerows(zip(*(raw.vectors[name] for name in chosen)))
    return str(path)

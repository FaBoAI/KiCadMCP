import csv
import math
from pathlib import Path
import struct
import tempfile
import unittest

from kicad_mcp.raw import RawFormatError, read_raw, summarize_raw, write_csv


def make_raw(path, rows=((0., 0., 2.), (.25, 1., 4.), (1., 2., 8.)), *, encoding='utf-8', newline='\n', flags='real', storage='binary', source='LTspice', force_double=False):
    lines = ['Title: synthetic RC test', 'Plotname: Transient Analysis', f'Flags: {flags}',
             'No. Variables: 3', f'No. Points: {len(rows)}', f'Command: {source}', 'Variables:',
             '\t0\ttime\ttime', '\t1\tV(out)\tvoltage', '\t2\tI(V1)\tdevice_current',
             'Binary:' if storage == 'binary' else 'Values:']
    header = (newline.join(lines) + newline).encode(encoding)
    if storage == 'binary':
        fmt = '<ddd' if force_double or 'double' in flags or source == 'ngspice' else '<dff'
        payload = b''.join(struct.pack(fmt, *row) for row in rows)
    else:
        payload = ''.join(f'{i}\t{row[0]}\n\t{row[1]}\n\t{row[2]}\n' for i, row in enumerate(rows)).encode(encoding)
    path.write_bytes(header + payload)
    return path


class RawTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'synthetic.raw'

    def test_ltspice_mixed_utf16_crlf(self):
        raw = read_raw(make_raw(self.path, encoding='utf-16-le', newline='\r\n'))
        self.assertEqual(raw.variables, ('time', 'V(out)', 'I(V1)'))
        self.assertEqual(list(raw.vectors['V(out)']), [0, 1, 2])
        self.assertEqual(raw.storage, 'ltspice-mixed')

    def test_double_and_ngspice(self):
        for source, flags in [('LTspice', 'real double'), ('ngspice', 'real')]:
            with self.subTest(source=source):
                raw = read_raw(make_raw(self.path, source=source, flags=flags))
                self.assertEqual(list(raw.vectors['I(V1)']), [2, 4, 8])

    def test_explicit_ngspice_and_wrong_layout(self):
        make_raw(self.path, source='unknown', force_double=True)
        with self.assertRaisesRegex(RawFormatError, 'size mismatch'):
            read_raw(self.path)
        self.assertEqual(read_raw(self.path, binary_format='ngspice').storage, 'ngspice-double')

    def test_ascii_encodings(self):
        for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                make_raw(self.path, storage='ascii', encoding=encoding)
                if encoding == 'utf-16-be':
                    self.path.write_bytes(b'\xfe\xff' + self.path.read_bytes())
                self.assertEqual(list(read_raw(self.path).axis), [0., .25, 1.])

    def test_truncated_and_appended_payload(self):
        original = make_raw(self.path).read_bytes()
        for contents in (original[:-1], original + b'x', original + original):
            self.path.write_bytes(contents)
            with self.assertRaisesRegex(RawFormatError, 'size mismatch'):
                read_raw(self.path)

    def test_complex_and_fastaccess_rejected(self):
        for flags in ('complex', 'real fastaccess'):
            with self.subTest(flags=flags):
                make_raw(self.path, flags=flags)
                with self.assertRaisesRegex(RawFormatError, 'not supported'):
                    read_raw(self.path)

    def test_ascii_count_index_and_value_validation(self):
        original = make_raw(self.path, storage='ascii').read_bytes()
        for contents in (original + b'1', original.replace(b'1\t0.25', b'9\t0.25'), original.replace(b'0\t0.0', b'0\tbad')):
            self.path.write_bytes(contents)
            with self.assertRaises(RawFormatError):
                read_raw(self.path)

    def test_bad_header_table_counts_duplicates(self):
        original = make_raw(self.path).read_bytes()
        variants = [original.replace(b'No. Points: 3', b'No. Points: -1'),
                    original.replace(b'\t2\tI(V1)', b'\t9\tI(V1)'),
                    original.replace(b'\t2\tI(V1)', b'\t2\tv(OUT)'),
                    original.replace(b'Flags: real', b'Flags: undefined'), b'nonsense']
        for contents in variants:
            self.path.write_bytes(contents)
            with self.assertRaises(RawFormatError):
                read_raw(self.path)

    def test_adaptive_time_weighting_interpolated_boundaries(self):
        make_raw(self.path)
        result = summarize_raw(self.path, ['v(OUT)'], .125, .625)
        signal = result['signals']['V(out)']
        self.assertEqual(signal['min'], .5)
        self.assertEqual(signal['max'], 1.5)
        self.assertAlmostEqual(signal['axis_weighted_mean'], 1.125)
        # Arithmetic average of the native sample values is not substituted.
        full = summarize_raw(self.path)['signals']['V(out)']
        self.assertAlmostEqual(full['axis_weighted_mean'], 1.25)

    def test_outside_missing_duplicate_and_nonmonotonic(self):
        make_raw(self.path)
        for kwargs in ({'start': -.1}, {'end': 1.1}, {'start': .5, 'end': .2}, {'signals': ['missing']}, {'signals': ['v(out)', 'V(out)']}):
            with self.assertRaises(ValueError):
                summarize_raw(self.path, **kwargs)
        make_raw(self.path, rows=((0., 0., 0.), (1., 1., 1.), (0., 2., 2.)))
        with self.assertRaisesRegex(RawFormatError, 'strictly increasing'):
            summarize_raw(self.path)

    def test_nonfinite_is_not_a_finite_statistic(self):
        make_raw(self.path, rows=((0., 0., 0.), (.5, math.nan, 1.), (1., 2., 2.)))
        result = summarize_raw(self.path)
        self.assertFalse(result['all_finite'])
        self.assertIsNone(result['signals']['V(out)']['max'])

    def test_csv_keeps_native_axis_and_protects_output(self):
        make_raw(self.path)
        output = Path(self.directory.name) / 'native.csv'
        write_csv(self.path, output, ['v(out)'])
        with output.open() as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ['time', 'V(out)'])
        self.assertEqual(rows[2], ['0.25', '1.0'])
        with self.assertRaises(FileExistsError):
            write_csv(self.path, output)


if __name__ == '__main__':
    unittest.main()

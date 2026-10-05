from pathlib import Path
import tempfile
import textwrap
import unittest
import sys

from kicad_mcp.spice import _command, _requested_stop, run_spice


class SpiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='spice tests ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.deck = self.root / 'synthetic.cir'
        self.deck.write_text('* synthetic RC\nV1 in 0 1\nR1 in out 1k\nC1 out 0 1u\n.tran 0 1m\n.end\n')
        self.out = self.root / 'outputs'

    def fake(self, mode='complete'):
        executable = self.root / 'fake simulator'
        executable.write_text(f'#!{sys.executable}\n' + textwrap.dedent(f'''
            from pathlib import Path
            import sys, time
            p = Path(sys.argv[-1])
            mode = {mode!r}
            if mode == 'timeout':
                print('running', flush=True)
                time.sleep(30)
            if mode == 'fatal':
                p.with_suffix('.log').write_text('Fatal error: synthetic failure\\n')
                sys.exit(0)
            if mode == 'exit':
                sys.exit(2)
            if mode != 'missing':
                end = 0.0005 if mode == 'partial' else 0.001
                value = 'nan' if mode == 'nonfinite' else '0.6321'
                p.with_suffix('.raw').write_text('Title: synthetic\\nPlotname: Transient Analysis\\nFlags: real\\nNo. Variables: 2\\nNo. Points: 2\\nVariables:\\n0 time time\\n1 V(out) voltage\\nValues:\\n0 0\\n0\\n1 ' + str(end) + '\\n' + value + '\\n')
            p.with_suffix('.log').write_text('Total elapsed time: 0.01 seconds\\n')
        '''))
        executable.chmod(0o755)
        return str(executable)

    def run_fake(self, mode='complete', **kwargs):
        return run_spice(str(self.deck), executable=self.fake(mode), output_dir=str(self.out), **kwargs)

    def test_complete_single_transient(self):
        result = self.run_fake()
        self.assertTrue(result['completed'])
        self.assertTrue(result['numerical_complete'])
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['requested_stop_s'], .001)
        self.assertEqual(result['raw_info']['point_count'], 2)
        self.assertNotEqual(result['netlist_path'], str(self.deck))

    def test_partial_fatal_missing_nonfinite_and_nonzero_are_not_complete(self):
        for mode in ('partial', 'fatal', 'missing', 'nonfinite', 'exit'):
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertFalse(result['completed'])
                if mode == 'fatal':
                    self.assertTrue(result['fatal'])
                    self.assertEqual(result['returncode'], 0)
                if mode == 'partial':
                    self.assertTrue(result['process_success'])
                    self.assertFalse(result['numerical_complete'])

    def test_timeout_is_explicit_and_keeps_logs(self):
        result = self.run_fake('timeout', timeout=.1)
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['completed'])
        self.assertIsNotNone(result['termination'])
        self.assertTrue(Path(result['launcher_log_path']).exists())

    def test_old_evidence_never_reused_or_deleted(self):
        old = self.deck.with_suffix('.raw')
        old.write_text('do not replace')
        first = self.run_fake()
        second = self.run_fake('missing')
        self.assertNotEqual(first['run_dir'], second['run_dir'])
        self.assertEqual(old.read_text(), 'do not replace')
        self.assertTrue(Path(first['raw_path']).exists())
        self.assertFalse(second['completed'])

    def test_relative_local_include_resolved_without_copying_models(self):
        model = self.root / 'synthetic model.inc'
        model.write_text('* synthetic test component\nR2 out 0 100k\n')
        self.deck.write_text('* test\n.include "synthetic model.inc"\n.lib vendor_library.lib\n.tran 1m\n.end\n')
        result = self.run_fake()
        staged = Path(result['netlist_path']).read_text()
        self.assertIn(f'.include "{model.resolve()}"', staged)
        self.assertIn('.lib vendor_library.lib', staged)
        self.assertEqual(len(result['include_rewrites']), 1)
        self.assertFalse((Path(result['run_dir']) / model.name).exists())

    def test_unknown_completion_scope_is_not_pass(self):
        self.deck.write_text('* test\n.param stop=1m\n.tran {{stop}}\n.end\n')
        result = self.run_fake()
        self.assertTrue(result['process_success'])
        self.assertIsNone(result['numerical_complete'])
        self.assertFalse(result['completed'])

    def test_expected_stop_scope(self):
        for command, engine, expected in [('.tran 10m', 'ltspice', .01), ('.tran 1u 10m 0 1u', 'ngspice', .01), ('.tran 0 10m startup', 'ltspice', .01), ('.tran 1m\n.step param R 1 2 1', 'ltspice', None), ('.tran {T}', 'ltspice', None), ('.tran 1m\n.op', 'ltspice', None), ('.control\nrun\n.endc', 'ngspice', None)]:
            with self.subTest(command=command):
                self.assertEqual(_requested_stop(command, engine)[0], expected)

    def test_ngspice_command_and_windows_bundle_path(self):
        command, wine = _command('ngspice', Path('/usr/bin/ngspice'), self.deck)
        self.assertIn('-n', command)
        self.assertIn('-r', command)
        self.assertFalse(wine)
        app = self.root / 'LTspice.app'
        launcher = app / 'Contents/SharedSupport/ltspice/bin/wine'
        launcher.parent.mkdir(parents=True)
        launcher.touch()
        command, wine = _command('ltspice', app, self.deck)
        self.assertTrue(wine)
        self.assertIn('--bottle', command)
        self.assertIn('-Run', command)
        self.assertTrue(command[-1].startswith(('Y:', 'Z:')))

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.run_fake(timeout=0)
        with self.assertRaises(ValueError):
            self.run_fake(engine='unknown')
        with self.assertRaises(FileNotFoundError):
            run_spice(str(self.deck), executable=str(self.root / 'missing'))
        other = self.root / 'not-a-deck.asc'
        other.write_text('test')
        with self.assertRaises(ValueError):
            run_spice(str(other), executable=self.fake())


if __name__ == '__main__':
    unittest.main()

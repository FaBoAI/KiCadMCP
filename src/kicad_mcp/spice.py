"""Explicit batch SPICE execution with provenance and conservative completion flags."""
from __future__ import annotations

from pathlib import Path
import hashlib
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

from .raw import RawFormatError, read_raw


_FATAL = re.compile(r'(?im)^\s*(?:fatal(?: error)?\b|error\b)|timestep too small|simulation(?:\(s\))? aborted|unknown subcircuit|time step too small|fatal error', re.I)


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-16-le' if (len(data) >= 4 and data[1] == 0 and data[3] == 0) else 'utf-8-sig'
    return data.decode(encoding, errors='replace')


def _number(token: str) -> float | None:
    match = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|mil|[tgkmunpf])?(?:s)?', token, re.I)
    if not match:
        return None
    scale = {'t': 1e12, 'g': 1e9, 'meg': 1e6, 'k': 1e3, 'm': 1e-3, 'u': 1e-6, 'n': 1e-9, 'p': 1e-12, 'f': 1e-15, 'mil': 25.4e-6}
    value = float(match[1]) * scale.get((match[2] or '').lower(), 1)
    return value if math.isfinite(value) else None


def _requested_stop(text: str, engine: str) -> tuple[float | None, str]:
    lines = [line.split(';', 1)[0].strip() for line in text.splitlines() if not line.lstrip().startswith('*')]
    if any(re.match(r'\.(?:step|control|ac|dc|op|noise|tf)\b', line, re.I) for line in lines):
        return None, 'Only one unstepped literal .tran has automatic end-time verification'
    analyses = [line for line in lines if re.match(r'\.tran\s+', line, re.I)]
    if len(analyses) != 1:
        return None, 'Exactly one literal .tran is required for automatic end-time verification'
    tokens = analyses[0].split()[1:]
    tokens = [token for token in tokens if token.lower() not in ('uic', 'startup', 'steady', 'nodiscard')]
    if not tokens or (engine == 'ngspice' and len(tokens) < 2):
        return None, 'Cannot determine .tran stop time'
    stop = _number(tokens[0] if len(tokens) == 1 else tokens[1])
    if stop is None or stop <= 0:
        return None, 'Parameterized/expression .tran stop times need independent verification'
    return stop, 'single literal .tran; last saved time and finite values checked'


def _windows_path(path: Path) -> str:
    # The official LTspice 26 macOS bundle maps home to Y: and Unix root to Z:.
    try:
        return 'Y:\\' + str(path.relative_to(Path.home())).replace('/', '\\')
    except ValueError:
        return 'Z:' + str(path).replace('/', '\\')


def _executable(engine: str, executable: str | None) -> Path:
    if executable:
        found = Path(executable).expanduser()
        if not found.exists():
            located = shutil.which(executable)
            if located:
                found = Path(located)
        if not found.exists():
            raise FileNotFoundError(f'SPICE executable/app not found: {executable}')
        return found.resolve()
    env_name = 'LTSPICE_EXECUTABLE' if engine == 'ltspice' else 'NGSPICE_EXECUTABLE'
    if os.environ.get(env_name):
        return _executable(engine, os.environ[env_name])
    candidates = ([Path('/Applications/LTspice.app'), Path.home() / 'Applications/LTspice.app'] if engine == 'ltspice' else [])
    for path in candidates:
        if path.exists():
            return path
    for name in (('ltspice', 'LTspice', 'LTspice.exe', 'XVIIx64.exe') if engine == 'ltspice' else ('ngspice',)):
        if located := shutil.which(name):
            return Path(located).resolve()
    raise FileNotFoundError(f'{engine} not found; specify executable or {env_name}')


def _command(engine: str, executable: Path, deck: Path) -> tuple[list[str], bool]:
    if engine == 'ngspice':
        return [str(executable), '-b', '-n', '-r', str(deck.with_suffix('.raw')), '-o', str(deck.with_suffix('.log')), str(deck)], False
    if executable.suffix.lower() == '.app':
        wine = executable / 'Contents/SharedSupport/ltspice/bin/wine'
        if wine.is_file():
            return [str(wine), '--bottle', 'ltspice', '--wait', '--workdir', 'C:/Program Files/ADI/LTspice', 'LTspice.exe', '-b', '-Run', _windows_path(deck)], True
        for binary in ('LTspice', 'LTspiceXVII'):
            native = executable / 'Contents/MacOS' / binary
            if native.is_file():
                return [str(native), '-b', str(deck)], False
        raise FileNotFoundError('No supported LTspice executable inside this .app bundle')
    return [str(executable), '-b', str(deck)], False


def _stage(text: str, original: Path, windows: bool) -> tuple[str, list[dict]]:
    rewrites = []
    result = []
    pattern = re.compile(r'^(\s*\.(?:include|inc|lib)\s+)(?:"([^"]+)"|\x27([^\x27]+)\x27|(\S+))(.*)$', re.I)
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            target = match[2] or match[3] or match[4]
            local = Path(target).expanduser()
            local = local if local.is_absolute() else original.parent / local
            # Keep simulator library-search names and .lib section declarations.
            if local.is_file():
                absolute = local.resolve()
                replacement = _windows_path(absolute) if windows else str(absolute)
                if '\n' in replacement or '"' in replacement:
                    raise ValueError('An include path contains an unsupported newline or double quote')
                line = match[1] + '"' + replacement + '"' + match[5]
                rewrites.append({'original': target, 'resolved': str(absolute)})
        result.append(line)
    return '\n'.join(result) + '\n', rewrites


def _terminate_owned_process(process: subprocess.Popen) -> str:
    """Terminate this run's process group; never kill a shared Wine server."""
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=2)
        return 'terminated owned process group' if os.name == 'posix' else 'terminated direct child; detached children not managed'
    except ProcessLookupError:
        return 'process already exited'
    except subprocess.TimeoutExpired:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=2)
        return 'killed owned process group' if os.name == 'posix' else 'killed direct child; detached children not managed'


def run_spice(netlist: str, engine: str = 'ltspice', executable: str | None = None,
              timeout: float = 60, output_dir: str | None = None) -> dict:
    """Run a trusted existing netlist once, saving all evidence in a fresh folder.

    This launches a simulator, not a sandbox. SPICE control blocks/model DLLs can
    perform local actions. No pass claim is made for model fidelity or hardware.
    """
    if engine not in ('ltspice', 'ngspice'):
        raise ValueError('engine must be ltspice or ngspice')
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a positive finite number')
    source = Path(netlist).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in ('.cir', '.sp', '.spice', '.net', '.ckt'):
        raise ValueError('netlist must be a .cir, .sp, .spice, .net, or .ckt file (not a KiCad schematic/.asc)')
    binary = _executable(engine, executable)
    destination = Path(output_dir).expanduser().resolve() if output_dir else None
    if destination:
        destination.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='spice-run-', dir=destination)).resolve()
    deck = run_dir / 'simulation.cir'
    command, wine = _command(engine, binary, deck)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original_text = _read_text(source)
    staged, rewrites = _stage(original_text, source, wine)
    deck.write_text(staged, encoding='utf-8')
    expected_stop, verification = _requested_stop(original_text, engine)
    launcher = run_dir / 'launcher.log'
    start_clock = time.monotonic()
    timed_out = False
    termination = None
    with launcher.open('wb') as stream:
        process = subprocess.Popen(command, cwd=source.parent, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=(os.name == 'posix'))
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            termination = _terminate_owned_process(process)
    log_path = deck.with_suffix('.log')
    raw_path = deck.with_suffix('.raw')
    log = _read_text(log_path) if log_path.is_file() else ''
    launcher_log = _read_text(launcher)
    fatal_lines = [line for line in (log + '\n' + launcher_log).splitlines() if _FATAL.search(line)]
    raw_valid = False
    raw_error = None
    raw_info = None
    numerical_complete = None
    if raw_path.is_file():
        try:
            raw = read_raw(raw_path, binary_format=engine)
            raw_valid = True
            finite = all(math.isfinite(value) for vector in raw.vectors.values() for value in vector)
            raw_info = {'point_count': raw.point_count, 'variables': list(raw.variables), 'storage': raw.storage,
                        'plot_name': raw.plot_name, 'all_finite': finite,
                        'first_axis': raw.axis[0] if math.isfinite(raw.axis[0]) else None,
                        'last_axis': raw.axis[-1] if math.isfinite(raw.axis[-1]) else None}
            if expected_stop is not None:
                monotonic = all(b > a for a, b in zip(raw.axis, raw.axis[1:]))
                numerical_complete = bool(finite and raw.variables[0].lower() == 'time' and monotonic and
                                          math.isclose(raw.axis[-1], expected_stop, rel_tol=1e-6, abs_tol=max(1e-15, expected_stop * 1e-9)))
        except (RawFormatError, OSError) as exc:
            raw_error = str(exc)
            numerical_complete = False
    else:
        raw_error = 'Simulator did not produce a RAW file'
        numerical_complete = False
    native_completion_marker = 'total elapsed time:' in log.lower() if engine == 'ltspice' else 'no. of data rows' in log.lower()
    process_success = process.returncode == 0 and not timed_out and not fatal_lines
    completed = bool(process_success and raw_valid and numerical_complete is True)
    return {'engine': engine, 'executable': str(binary), 'command': command,
            'source_netlist': str(source), 'source_sha256': source_hash,
            'staged_netlist_sha256': hashlib.sha256(deck.read_bytes()).hexdigest(),
            'run_dir': str(run_dir), 'netlist_path': str(deck), 'include_rewrites': rewrites,
            'log_path': str(log_path) if log_path.exists() else None, 'launcher_log_path': str(launcher),
            'raw_path': str(raw_path) if raw_path.exists() else None,
            'returncode': process.returncode, 'elapsed_s': time.monotonic() - start_clock,
            'timed_out': timed_out, 'termination': termination,
            'process_success': process_success, 'fatal': bool(fatal_lines), 'fatal_lines': fatal_lines,
            'native_completion_marker': native_completion_marker, 'raw_valid': raw_valid, 'raw_error': raw_error,
            'raw_info': raw_info, 'requested_stop_s': expected_stop,
            'numerical_complete': numerical_complete, 'completed': completed,
            'verification_scope': verification,
            'limitation': 'Completion is numerical execution evidence, not circuit/model/hardware validation. Detached simulator children may outlive a timeout; shared servers are never killed.'}

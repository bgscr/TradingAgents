"""Focused stdlib-only tests. All tokens are synthetic; no application/API runs.

Run: python -I -S -B tests/test_session_token_pipe.py
This is not a substitute for the project's full acceptance or Windows testing.
"""
from __future__ import annotations

import contextlib
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
SCRIPTS = SOURCE / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import session_token_pipe as pipe
import accept_financial_statements as financial
import analyze_mainland_bounded as entry
import diagnostic_runtime as runtime

DUMMY = 'SYNTHETIC_NOT_A_REAL_TUSHARE_TOKEN'
DUMMY_MODEL = 'SYNTHETIC_NOT_A_REAL_MODEL_KEY'
RUN_ID = '0123456789abcdef0123456789abcdef'


def offline_audit(event, args):
    if event.startswith('socket.') or event in {'http.client.connect', 'http.client.send'}:
        raise RuntimeError('offline_test_network_forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0]))
        if any(part.lower() in {'.ssh', '.aws', '.azure'} or part.lower().startswith('.env')
               or part.lower() == 'tradingagents.local.json' for part in path.parts):
            raise RuntimeError('offline_test_credential_file_forbidden')


def fresh_pipe(data):
    reader, writer = os.pipe()
    try:
        os.write(writer, data)
    finally:
        os.close(writer)
    return os.fdopen(reader, 'rb')


def owned_root(source):
    root = source / 'reports/bounded-runs' / RUN_ID / 'refresh'
    root.parent.mkdir(parents=True)
    (root.parent / 'session.json').write_text(json.dumps({
        'contract': 'bounded-mainland-session-v1', 'run_id': RUN_ID,
        'root': str(root.parent), 'resume': False}), encoding='utf-8')
    return root


class FrameTests(unittest.TestCase):
    def test_round_trip_and_close(self):
        stream = fresh_pipe(pipe.encode_session_token(DUMMY, RUN_ID))
        self.assertEqual(pipe.read_session_token(stream, RUN_ID), DUMMY)
        self.assertTrue(stream.closed)

    def test_maximum_token(self):
        with fresh_pipe(pipe.encode_session_token('x' * 512, RUN_ID)) as stream:
            self.assertEqual(len(pipe.read_session_token(stream, RUN_ID)), 512)

    def test_reject_invalid_token_without_echo(self):
        for value in ['', 'x' * 513, 'a b', 'a\nb', '\x00x', '非ASCII', None]:
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(pipe.TokenPipeError) as caught:
                    pipe.encode_session_token(value, RUN_ID)
                self.assertEqual(str(caught.exception), 'session_token_input_invalid')

    def test_invalid_run_ids(self):
        for run in ['', '../other', 'A' * 32, 'z' * 32, None]:
            with self.assertRaises(pipe.TokenPipeError):
                pipe.encode_session_token(DUMMY, run)

    def test_reject_invalid_frames_and_close(self):
        valid = pipe.encode_session_token(DUMMY, RUN_ID)
        frames = [b'', valid[:-1], valid + b'extra', valid + b'\n', b'x' * 1000,
                  valid.replace(RUN_ID.encode(), b'1' * 32),
                  pipe.PREFIX + RUN_ID.encode() + b'\n\xff\n',
                  pipe.PREFIX + RUN_ID.encode() + b'\n\n']
        for frame in frames:
            stream = fresh_pipe(frame)
            with self.assertRaises(pipe.TokenPipeError):
                pipe.read_session_token(stream, RUN_ID)
            self.assertTrue(stream.closed)

    def test_reject_regular_file_or_memory_stream(self):
        with tempfile.TemporaryFile('w+b') as stream:
            stream.write(pipe.encode_session_token(DUMMY, RUN_ID))
            stream.seek(0)
            with self.assertRaisesRegex(pipe.TokenPipeError, 'pipe_required'):
                pipe.read_session_token(stream, RUN_ID)
        with self.assertRaisesRegex(pipe.TokenPipeError, 'pipe_required'):
            pipe.require_pipe(io.BytesIO())

    def test_require_pipe_does_not_read(self):
        stream = fresh_pipe(pipe.encode_session_token(DUMMY, RUN_ID))
        pipe.require_pipe(stream)
        self.assertEqual(pipe.read_session_token(stream, RUN_ID), DUMMY)


class LifecycleTests(unittest.TestCase):
    def command(self, mode):
        return [sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve()), mode]

    def test_real_child_frame_transport_and_clean_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = pipe.send_session_token(self.command('--receive-dummy'),
                token=DUMMY, run_id=RUN_ID, cwd=SOURCE, env=runtime.safe_environment(tmp))
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn(DUMMY, repr(completed))

    def test_early_child_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = pipe.send_session_token(self.command('--early-exit'),
                token=DUMMY, run_id=RUN_ID, cwd=SOURCE, env=runtime.safe_environment(tmp))
        self.assertEqual(completed.returncode, 7)

    def test_real_three_process_handoff_and_reuse_devnull(self):
        with tempfile.TemporaryDirectory() as tmp:
            completed = pipe.send_session_token(self.command('--coordinator-dummy') + [tmp],
                token=DUMMY, run_id=RUN_ID, cwd=SOURCE, env=runtime.safe_environment(tmp))
        self.assertEqual(completed.returncode, 0)

    def test_missing_eof_timeout_kills_and_reaps(self):
        reader, writer = os.pipe()
        process_class = subprocess.Popen
        try:
            with os.fdopen(reader, 'rb') as stream, tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(pipe.subprocess, 'Popen', wraps=process_class) as spawn:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        pipe.run_worker(self.command('--receive-dummy'), cwd=SOURCE,
                            env=runtime.safe_environment(tmp), stdin=stream, timeout=0.15)
                    self.assertEqual(spawn.call_count, 1)
                    self.assertTrue(spawn.call_args.kwargs['close_fds'])
        finally:
            os.close(writer)

    def test_worker_interrupt_kills_and_reaps(self):
        process = mock.Mock()
        process.wait.side_effect = [KeyboardInterrupt, 9]
        process.poll.return_value = None
        with mock.patch.object(pipe.subprocess, 'Popen', return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                pipe.run_worker(['fixed-child'], cwd=SOURCE, env={}, timeout=1)
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)

    def test_repeated_interrupt_during_kill_still_reaps(self):
        process = mock.Mock()
        process.wait.side_effect = [KeyboardInterrupt, KeyboardInterrupt, 9]
        process.poll.return_value = None
        process.kill.side_effect = [KeyboardInterrupt, None]
        with mock.patch.object(pipe.subprocess, 'Popen', return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                pipe.run_worker(['fixed-child'], cwd=SOURCE, env={}, timeout=1)
        self.assertEqual(process.kill.call_count, 2)
        self.assertEqual(process.wait.call_count, 3)

    def test_parent_interrupt_closes_and_waits_without_killing_coordinator(self):
        process = mock.Mock()
        process.stdin.closed = False
        process.communicate.side_effect = KeyboardInterrupt
        process.wait.side_effect = [KeyboardInterrupt, 1]
        with mock.patch.object(pipe.subprocess, 'Popen', return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                pipe.send_session_token(['fixed-coordinator'], token=DUMMY, run_id=RUN_ID,
                                        cwd=SOURCE, env={})
        process.stdin.close.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(process.wait.call_count, 2)

    def test_existing_guard_allows_pipe_but_still_denies_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = self.command('--guarded-dummy') + [tmp]
            completed = pipe.send_session_token(command, token=DUMMY, run_id=RUN_ID,
                cwd=SOURCE, env=runtime.safe_environment(tmp))
        self.assertEqual(completed.returncode, 0)


class EntryTests(unittest.TestCase):
    def test_refresh_passes_only_tushare_over_pipe(self):
        session = entry.Session(SOURCE / 'reports/bounded-runs' / RUN_ID)
        session.selection = {'ticker': '600895.SH', 'analysis_date': '2026-11-06'}
        session.clock = types.SimpleNamespace(remaining=lambda: 120,
            effective_deadline=lambda: time.time() + 120, deadline_monotonic=time.monotonic() + 120)
        session.verify_refresh = lambda: [1, 0]
        with mock.patch.object(entry.os, 'environ', {'TUSHARE_TOKEN': DUMMY,
                'DEEPSEEK_API_KEY': DUMMY_MODEL}), mock.patch.object(pipe, 'send_session_token',
                return_value=types.SimpleNamespace(returncode=0)) as send:
            self.assertEqual(session.refresh(), [1, 0])
        args, kwargs = send.call_args
        self.assertIn('--session-token-stdin', args[0])
        self.assertNotIn(DUMMY, repr(args[0]))
        self.assertNotIn(DUMMY_MODEL, repr(args) + repr(kwargs))
        self.assertEqual(kwargs['token'], DUMMY)
        self.assertEqual(kwargs['run_id'], RUN_ID)
        self.assertNotIn('TUSHARE_TOKEN', kwargs['env'])
        self.assertNotIn('DEEPSEEK_API_KEY', kwargs['env'])

    def test_hidden_entry_prompts_once_per_key(self):
        prompts = []
        def fake_prompt(label):
            prompts.append(label)
            return DUMMY if label.startswith('Tushare') else DUMMY_MODEL
        with mock.patch.object(entry.os, 'environ', {}), mock.patch('getpass.getpass', fake_prompt), \
             mock.patch.object(sys, 'stdin', types.SimpleNamespace(isatty=lambda: True)):
            entry.read_user_credentials()
        self.assertEqual(sum(label.startswith('Tushare') for label in prompts), 1)
        self.assertEqual(sum(label.startswith('DeepSeek') for label in prompts), 1)


class BindingTests(unittest.TestCase):
    def test_owned_session_only_and_record_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            root = owned_root(source)
            with mock.patch.object(financial, 'SOURCE', source):
                self.assertEqual(financial.session_pipe_run_id(root,
                    destination=financial.PROJECT_ENV_DESTINATION, live=True, binding={}), RUN_ID)
                for destination, live, binding in [
                    ('https://api.tushare.pro', True, {}),
                    (financial.PROJECT_ENV_DESTINATION, False, {}),
                    (financial.PROJECT_ENV_DESTINATION, True, None)]:
                    with self.assertRaises(ValueError):
                        financial.session_pipe_run_id(root, destination=destination,
                                                      live=live, binding=binding)
                (root.parent / 'session.json').write_text('{}', encoding='utf-8')
                with self.assertRaises(ValueError):
                    financial.session_pipe_run_id(root,
                        destination=financial.PROJECT_ENV_DESTINATION, live=True, binding={})

    def test_external_output_not_allowed_for_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                financial.session_pipe_run_id(Path(tmp) / 'refresh',
                    destination=financial.PROJECT_ENV_DESTINATION, live=True, binding={})

    def test_worker_deadline_never_expands(self):
        self.assertEqual(financial._worker_timeout(200, 1200, now=100, monotonic_now=1100), 100)
        self.assertEqual(financial._worker_timeout(2000, 3000, now=100, monotonic_now=1100), 180)
        self.assertEqual(financial._worker_timeout(99, 1200, now=100, monotonic_now=1100), 0)


class WorkerTests(unittest.TestCase):
    def test_invalid_context_stops_before_install_or_token_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            root = owned_root(source)
            root.mkdir()
            (root.parent / 'session.json').write_text('{}', encoding='utf-8')
            contract = {'sources': {'dummy': 'hash'}, 'live': True,
                'credential_mode': 'session_pipe',
                'request': {'destination': financial.PROJECT_ENV_DESTINATION,
                            'shared_binding': {'storage': {}}},
                'initialization_policy': financial._initialization_grant()}
            (root / 'contract.json').write_text(json.dumps(contract), encoding='utf-8')
            with mock.patch.object(financial, 'SOURCE', source), \
                 mock.patch.object(financial, 'identities', return_value={'dummy': 'hash'}), \
                 mock.patch.object(financial, 'SharedStatementStorage', return_value=object()), \
                 mock.patch.object(financial, 'install') as install, \
                 mock.patch.object(financial, '_worker') as worker, \
                 mock.patch.object(pipe, 'read_session_token') as read:
                self.assertEqual(financial.worker(types.SimpleNamespace(output=root, worker='live')), 1)
                install.assert_not_called()
                worker.assert_not_called()
                read.assert_not_called()

    def application(self, token_frame, *, expired=False):
        acceptance = types.ModuleType('tradingagents.statement_acceptance')
        acceptance.validate_request = lambda *a, **k: (None, types.SimpleNamespace(
            instrument_identity=types.SimpleNamespace(symbol='600895.SS')))
        acquire = mock.Mock(return_value={'status': 'acquired', 'physical_requests': 1})
        acceptance.acquire = acquire
        package = types.ModuleType('tradingagents')
        package.statement_acceptance = acceptance
        contract = {'request': {}, 'credential_mode': 'session_pipe',
                    'session_deadline_epoch': time.time() + (-1 if expired else 60),
                    'session_monotonic_deadline': time.monotonic() + (-1 if expired else 60)}
        stream = fresh_pipe(token_frame)
        wrapper = types.SimpleNamespace(buffer=stream)
        with mock.patch.dict(sys.modules, {'tradingagents': package,
                    'tradingagents.statement_acceptance': acceptance}), \
             mock.patch.object(sys, 'stdin', wrapper), \
             mock.patch('getpass.getpass', side_effect=AssertionError('unexpected_prompt')):
            try:
                result = financial._worker(types.SimpleNamespace(worker='live'),
                    Path('/unused') / RUN_ID / 'refresh', contract, {},
                    types.SimpleNamespace(exchange=lambda *a: contextlib.nullcontext()), {})
                return result, acquire
            finally:
                stream.close()

    def test_live_uses_supplied_token_without_prompt(self):
        _, acquire = self.application(pipe.encode_session_token(DUMMY, RUN_ID))
        self.assertEqual(acquire.call_args.kwargs['token'], DUMMY)
        self.assertEqual(acquire.call_count, 1)

    def test_invalid_or_missing_pipe_input_never_prompts(self):
        for frame in [b'', b'bad', pipe.encode_session_token(DUMMY, '1' * 32)]:
            with self.assertRaises(pipe.TokenPipeError):
                self.application(frame)

    def test_expired_session_does_not_consume_token(self):
        with mock.patch.object(pipe, 'read_session_token') as read:
            with self.assertRaisesRegex(RuntimeError, 'deadline_exceeded'):
                self.application(pipe.encode_session_token(DUMMY, RUN_ID), expired=True)
            read.assert_not_called()


class CoordinatorTests(unittest.TestCase):
    def test_existing_original_failure_test_unchanged(self):
        # Execute the supplied original regression function with its assertions
        # unchanged. Only its missing full-project source inventory is stubbed;
        # the real CLI failure path and subprocess.run interception are tested.
        name = 'test_cli_failure_writes_terminal_receipt_and_does_not_launch_reuse'
        path = SOURCE / 'tests/test_financial_acceptance_scope.py'
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == name)
        namespace = {'json': json}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
        package = types.ModuleType('scripts')
        package.accept_financial_statements = financial
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            class MonkeyPatch:
                def setattr(self, target, attribute, value):
                    stack.enter_context(mock.patch.object(target, attribute, value))
            stack.enter_context(mock.patch.dict(sys.modules, {'scripts': package,
                'scripts.accept_financial_statements': financial}))
            stack.enter_context(mock.patch.object(financial, 'identities',
                                                  return_value={'offline_inventory': 'dummy'}))
            # A normal legacy run must not reach the new Popen supervision path.
            spawn = stack.enter_context(mock.patch.object(pipe, 'run_worker',
                side_effect=AssertionError('legacy_mode_used_pipe_supervisor')))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            namespace[name](Path(tmp), MonkeyPatch())
            spawn.assert_not_called()

    def test_coordinator_forwards_live_only_and_receipts_are_secret_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            root = owned_root(source)
            stream = io.TextIOWrapper(fresh_pipe(pipe.encode_session_token(DUMMY, RUN_ID)))
            phases = []
            views = {str(n): {'outcome': 'dummy'} for n in range(6)}
            def fake_run(command, **kwargs):
                phase = command[-1]
                phases.append(phase)
                self.assertNotIn(DUMMY, repr(command) + repr(kwargs['env']))
                if phase == 'live':
                    self.assertIs(kwargs['stdin'], stream.buffer)
                    self.assertEqual(pipe.read_session_token(kwargs['stdin'], RUN_ID), DUMMY)
                else:
                    self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                    self.assertTrue(stream.closed)
                receipt = {'status': 'acquired' if phase == 'live' else 'reused',
                    'physical_requests': 1 if phase == 'live' else 0,
                    'canonical_symbol': '600895.SS', 'pid': 101 if phase == 'live' else 102,
                    'client_attempts': 0, 'views': views,
                    'guard_probes': {'socket': 'blocked', 'environment': 'blocked', 'file': 'blocked'},
                    'import_access': {'credentials': 0},
                    'access_during_application': {'network': 0, 'credentials': 0,
                                                 'subprocess': 0, 'outside_write': 0}}
                (root / (phase + '.json')).write_text(json.dumps(receipt), encoding='utf-8')
                return types.SimpleNamespace(returncode=0)
            argv = ['accept_financial_statements.py', '--output', str(root), '--destination',
                financial.PROJECT_ENV_DESTINATION, '--account', 'personal-2000', '--ticker',
                '600895.SH', '--as-of', '2026-11-06', '--shared-binding', str(source / 'binding.json'),
                '--authorize-live', '--authorize-initialization-probes', '--session-token-stdin',
                '--session-deadline-epoch', str(time.time() + 90),
                '--session-monotonic-deadline', str(time.monotonic() + 90)]
            original_allowed = financial.output_path_allowed
            helpers = types.SimpleNamespace(validate_prepared_views=lambda *a, **k: 0,
                                            scopes_from_binding=lambda *a: None)
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(sys, 'stdin', stream), \
                 mock.patch.object(financial, 'SOURCE', source), \
                 mock.patch.object(financial, 'output_path_allowed',
                    side_effect=lambda root, **kwargs: original_allowed(root,
                        source=source, require_fresh=kwargs.get('require_fresh', True))), \
                 mock.patch.object(financial, 'load_shared_binding', return_value={}), \
                 mock.patch.object(financial, 'identities', return_value={'dummy': 'source'}), \
                 mock.patch.object(financial, '_initialization_verified', return_value=True), \
                 mock.patch.object(financial, '_receipt_helpers', return_value=helpers), \
                 mock.patch.object(pipe, 'run_worker', side_effect=fake_run), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(financial.main(), 0)
            self.assertEqual(phases, ['live', 'reuse'])
            text = output.getvalue() + ''.join(p.read_text() for p in root.glob('*.json'))
            self.assertNotIn(DUMMY, text)
            self.assertNotIn(DUMMY_MODEL, text)
            contract = json.loads((root / 'contract.json').read_text())
            self.assertEqual(contract['credential_mode'], 'session_pipe')
            self.assertEqual(contract['physical_request_limit'], 3)
            self.assertEqual(contract['retries'], 0)


def dummy_child(mode):
    if mode == '--early-exit':
        return 7
    if mode == '--coordinator-dummy':
        pipe.require_pipe(sys.stdin.buffer)
        command = [sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve())]
        env = runtime.safe_environment(sys.argv[2])
        try:
            live = pipe.run_worker(command + ['--receive-dummy'], cwd=SOURCE, env=env,
                                   stdin=sys.stdin.buffer, timeout=3)
        finally:
            sys.stdin.close()
        if live.returncode:
            return live.returncode
        return pipe.run_worker(command + ['--reuse-empty'], cwd=SOURCE, env=env,
                               stdin=subprocess.DEVNULL, timeout=3).returncode
    if mode == '--reuse-empty':
        assert sys.stdin.buffer.read(1) == b''
        assert 'TUSHARE_TOKEN' not in os.environ
        assert 'DEEPSEEK_API_KEY' not in os.environ
        return 0
    if mode == '--guarded-dummy':
        # Only replace unavailable curl bindings in this isolated test process.
        # The uploaded production guard itself is unmodified.
        curl = types.ModuleType('curl_cffi')
        curl.requests = types.SimpleNamespace(Session=type('Session', (), {}),
                                              AsyncSession=type('AsyncSession', (), {}))
        sys.modules['curl_cffi'] = curl
        counts, _ = runtime.install(Path(sys.argv[2]), SOURCE, offline=True)
        value = pipe.read_session_token(sys.stdin.buffer, RUN_ID)
        assert value == DUMMY
        del value
        try:
            os.getenv('TUSHARE_TOKEN')
        except runtime.DeniedAccess:
            pass
        else:
            return 4
        assert counts['credentials'] == 1
        return 0
    assert 'TUSHARE_TOKEN' not in os.environ
    assert 'DEEPSEEK_API_KEY' not in os.environ
    assert DUMMY not in repr(sys.argv)
    value = pipe.read_session_token(sys.stdin.buffer, RUN_ID)
    assert value == DUMMY
    del value
    return 0


if __name__ == '__main__':
    sys.addaudithook(offline_audit)
    if len(sys.argv) > 1 and sys.argv[1].startswith('--'):
        raise SystemExit(dummy_child(sys.argv[1]))
    unittest.main(verbosity=2)

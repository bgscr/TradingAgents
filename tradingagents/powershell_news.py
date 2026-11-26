"""Explicit, one-attempt PowerShell news transport; anonymous bounded IPC only."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode, quote

from tradingagents.dataflows.eastmoney_news import MAX_BODY_BYTES, news_query
from tradingagents.news_projection import (
    NewsRequest, _ticker, acquire_validated_news, unavailable, validate_response,
)

PROVIDER = 'eastmoney_powershell'
_ACTIVE = ContextVar('powershell_news', default=None)
_SYSTEM_ENV = ('SystemRoot', 'WINDIR', 'SystemDrive', 'ComSpec', 'PATH', 'PATHEXT',
               'PROCESSOR_ARCHITECTURE', 'PROCESSOR_ARCHITEW6432', 'NUMBER_OF_PROCESSORS')
_STATUSES = frozenset({'ok', 'input_invalid', 'runtime_unsupported', 'cmdlet_invalid',
                       'request_failed', 'http_error', 'response_limit'})
MAX_IPC = 1_500_000


class PowerShellNewsStop(BaseException):
    """Fixed local integrity/cleanup error; cannot become an optional data gap."""


def _ordinary_file(path):
    path = Path(path)
    if not path.is_absolute():
        raise PowerShellNewsStop('powershell_path_invalid')
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise PowerShellNewsStop('powershell_linked_path')
    if not path.is_file():
        raise PowerShellNewsStop('powershell_file_missing')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_request(ticker, as_of):
    symbol = _ticker(ticker)
    if type(as_of) is not str or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', as_of) is None:
        raise ValueError('news_selection_invalid')
    end = date.fromisoformat(as_of)
    callback = 'jQuery35101792940631092459_1764599530165'
    query = news_query(symbol[:6], callback, '1764599530176')
    return NewsRequest(symbol, (end - timedelta(days=7)).isoformat(), end.isoformat(), callback,
        'https://search-api-web.eastmoney.com/search/jsonp?' + urlencode(query, quote_via=quote))


def _child_environment(executable):
    result = {key: os.environ[key] for key in _SYSTEM_ENV if key in os.environ}
    # No inherited credential/proxy/profile/module variables; no browser state.
    result['PSModulePath'] = str(Path(executable).parent / 'Modules')
    result['POWERSHELL_TELEMETRY_OPTOUT'] = '1'
    result['POWERSHELL_UPDATECHECK'] = 'Off'
    return result


def _read_pipe(pipe, limit, chunks, state):
    try:
        while True:
            block = pipe.read(4096)
            if not block:
                break
            state['bytes'] += len(block)
            if state['bytes'] > limit:
                state['overflow'] = True
                break
            chunks.append(block)
    except BaseException:
        state['read_failed'] = True


def run_child(executable, script, url, *, remaining, check, launch=subprocess.Popen,
              clock=time.monotonic, metadata=None, before_request=None):
    """One owned process; kill/wait/close on all exits, no text in diagnostics.

    The cmdlet buffers in its child process; IPC/body admission is capped here,
    not a claim of a hard whole-process allocation limit. Wall time is bounded.
    """
    seconds = math.floor(min(20, remaining))
    if seconds < 1:
        return None, {'status': 'insufficient_time', 'attempted': False}
    deadline = clock() + min(25, remaining)
    command = [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)]
    process = None
    readers = []
    stdout, stderr = [], []
    out_state, err_state = ({'bytes': 0, 'overflow': False, 'read_failed': False} for _ in range(2))
    metadata = {} if metadata is None else metadata
    metadata.update(status='launch_failed', attempted=None, http_status=None, cleanup='not_opened',
                    process_started=False, stage='pre_launch')
    primary = None
    try:
        check()
        process = launch(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=_child_environment(executable), shell=False, close_fds=True,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), bufsize=0)
        metadata.update(status='running', process_started=True, stage='child_io')
        for pipe, limit, chunks, state in ((process.stdout, MAX_IPC, stdout, out_state),
                                           (process.stderr, 4096, stderr, err_state)):
            worker = threading.Thread(target=_read_pipe, args=(pipe, limit, chunks, state), daemon=True)
            worker.start()
            readers.append(worker)
        wire = json.dumps({'url': url, 'timeout_seconds': seconds}, ensure_ascii=True).encode('ascii') + b'\n'
        if len(wire) > 2048:
            raise PowerShellNewsStop('powershell_input_limit')
        if before_request is not None:
            check()
            metadata['request_submission'] = 'not_submitted'
            before_request()
            check()  # Durable attempt registration can wait for a database lock.
            metadata['request_submission'] = 'unknown'
        process.stdin.write(wire)
        process.stdin.close()
        if before_request is not None:
            metadata['request_submission'] = 'submitted'
        while process.poll() is None:
            check()
            if clock() >= deadline:
                metadata['status'] = 'timeout'
                return None, metadata
            if any(s['overflow'] or s['read_failed'] for s in (out_state, err_state)):
                metadata['status'] = 'ipc_limit_or_read_failure'
                return None, metadata
            time.sleep(0.01)
        for worker in readers:
            worker.join(timeout=1)
        if any(w.is_alive() for w in readers) or any(s['overflow'] or s['read_failed'] for s in (out_state, err_state)):
            metadata['status'] = 'ipc_limit_or_read_failure'
            return None, metadata
        if err_state['bytes']:
            metadata['status'] = 'child_stderr'
            return None, metadata
        metadata['status'] = 'ipc_invalid'
        metadata['stage'] = 'response'
        try:
            from tradingagents.dataflows.eastmoney_news import _pairs
            envelope = json.loads(b''.join(stdout).decode('utf-8'), object_pairs_hook=_pairs)
            if (type(envelope) is not dict or set(envelope) != {'status', 'http_status', 'body', 'cleanup', 'request_attempted'}
                    or envelope['status'] not in _STATUSES or type(envelope['request_attempted']) is not bool
                    or envelope['cleanup'] not in {'closed', 'not_opened', 'close_failed', 'pending'}
                    or (envelope['http_status'] is not None and (type(envelope['http_status']) is not int
                        or not 100 <= envelope['http_status'] <= 599))):
                return None, metadata
            metadata.update(status=envelope['status'], attempted=envelope['request_attempted'],
                            http_status=envelope['http_status'], cleanup=envelope['cleanup'])
            if (process.returncode != 0 or envelope['status'] != 'ok' or envelope['http_status'] != 200
                    or envelope['cleanup'] != 'closed' or envelope['request_attempted'] is not True):
                if envelope['status'] == 'ok':
                    metadata['status'] = ('child_nonzero' if process.returncode != 0 else
                                          'response_not_closed' if envelope['cleanup'] != 'closed' else
                                          'ipc_inconsistent')
                return None, metadata
            body = base64.b64decode(envelope['body'], validate=True)
            if len(body) > MAX_BODY_BYTES:
                metadata['status'] = 'response_limit'
                return None, metadata
            return body, metadata
        except (ValueError, TypeError, UnicodeError, RecursionError):
            metadata['status'] = 'ipc_invalid'
            return None, metadata
    except PermissionError:
        primary = PowerShellNewsStop('powershell_process_permission_denied')
        raise primary from None
    except (OSError, subprocess.SubprocessError):
        metadata['status'] = 'child_io_failed'
        return None, metadata
    except BaseException as error:
        primary = error
        metadata['status'] = 'interrupted'
        raise
    finally:
        # We only touch the process/handles this function created. No process scan.
        cleanup_failed = False
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    cleanup_failed = True
            for worker in readers:
                worker.join(timeout=1)
            cleanup_failed |= any(worker.is_alive() for worker in readers)
        metadata.update(stdout_bytes=out_state['bytes'], stderr_bytes=err_state['bytes'],
                        process_closed=not cleanup_failed)
        if cleanup_failed and primary is None:
            raise PowerShellNewsStop('powershell_child_cleanup_failed')


class PowerShellNews:
    """One selection, one attempt in the owning process; no automatic reprompt."""
    def __init__(self, executable, script, ticker, as_of, *, isolate_invalid_sources=False, as_known_on_date=False):
        if type(isolate_invalid_sources) is not bool:
            raise PowerShellNewsStop('invalid_source_isolation_mode')
        self.isolate_invalid_sources = isolate_invalid_sources
        self.executable, self.script = Path(executable), Path(script)
        if self.executable.name.lower() != 'pwsh.exe' or self.script.name != 'Get-PublicNews.ps1':
            raise PowerShellNewsStop('powershell_path_invalid')
        self.request = make_request(ticker, as_of)
        self.value = None
        self.attempted = False
        self.metadata = {'status': 'not_attempted'}
        self.lock = threading.Lock()
        try:
            self.binding = (_ordinary_file(self.executable), _ordinary_file(self.script))
        except FileNotFoundError:
            self.binding = None
            self.attempted = True
            self.metadata = {'status': 'runtime_missing', 'attempted': False}
            self.value = unavailable(self.request, 'powershell_runtime_missing', isolate_invalid_sources=self.isolate_invalid_sources)
        if as_known_on_date:
            self.attempted = True
            self.metadata = {'status': 'historical_availability_unverified', 'attempted': False}
            self.value = unavailable(self.request, 'historical_availability_unverified',
                                     isolate_invalid_sources=self.isolate_invalid_sources)

    def summary(self):
        admission = self.value.summary() if self.value else None
        if admission is not None:
            admission['provider'] = PROVIDER
            admission['transport_completed'] = self.metadata.get('status') == 'ok'
        return {'provider': PROVIDER, 'transport': dict(self.metadata),
                'cross_session_cache': {'status': 'miss', 'reason': 'raw_response_not_retained'},
                'admission': admission,
                'raw_body_retained': False, 'automatic_retry': False,
                'executable_sha256': self.binding[0] if self.binding else None,
                'script_sha256': self.binding[1] if self.binding else None}

    def acquire(self, ticker, start_date, end_date, **kwargs):
        from tradingagents.analysis_budget import active_budget, admitted_transport, require_data_vendor
        budget = active_budget()
        if budget is None:
            raise PowerShellNewsStop('powershell_budget_required')
        budget.check()
        # Wrong tool requests cannot spend the single fetch or use another stock.
        try:
            valid = (_ticker(ticker) == self.request.ticker
                     and all(type(day) is str and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', day)
                             for day in (start_date, end_date))
                     and date.fromisoformat(self.request.start) <= date.fromisoformat(start_date)
                     <= date.fromisoformat(end_date) <= date.fromisoformat(self.request.end))
            identity = kwargs.get('instrument_identity')
            valid = valid and (identity is None or _ticker(identity.symbol) == self.request.ticker)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            # The owner exists: distinguish an invalid query from absent context.
            # Reuse the projection boundary's strict query classification; no send.
            return acquire_validated_news(unavailable(self.request, 'request_invalid'),
                                          ticker, start_date, end_date, provider_name=PROVIDER, **kwargs)
        while not self.lock.acquire(timeout=0.05):
            budget.check()
        try:
            if not self.attempted:
                if self.binding != (_ordinary_file(self.executable), _ordinary_file(self.script)):
                    raise PowerShellNewsStop('powershell_source_changed')
                require_data_vendor('akshare')  # Same upstream; new transport is not a vendor bypass.
                self.attempted = True  # no refund/relaunch after uncertain failure.
                self.metadata = {'status': 'admission_pending', 'attempted': None, 'process_started': False}
                with admitted_transport('data') as remaining:
                    self.metadata['status'] = 'admitted'
                    body, self.metadata = run_child(self.executable, self.script, self.request.url,
                                                   remaining=remaining, check=budget.check, metadata=self.metadata)
                if body is None:
                    self.value = unavailable(self.request, 'powershell_' + self.metadata['status'], isolate_invalid_sources=self.isolate_invalid_sources)
                else:
                    try:
                        text = body.decode('utf-8-sig')
                    except UnicodeError:
                        self.value = unavailable(self.request, 'encoding_invalid', isolate_invalid_sources=self.isolate_invalid_sources)
                    else:
                        self.value = validate_response(self.request, text, isolate_invalid_sources=self.isolate_invalid_sources)
            return acquire_validated_news(self.value, ticker, start_date, end_date,
                                           provider_name=PROVIDER, **kwargs)
        finally:
            self.lock.release()


@contextmanager
def powershell_news_scope(value):
    token = _ACTIVE.set(value)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def acquire_powershell_news(*args, **kwargs):
    value = _ACTIVE.get()
    if value is None:
        return acquire_validated_news(None, *args, provider_name=PROVIDER, **kwargs)
    return value.acquire(*args, **kwargs)


def active_powershell_news():
    return _ACTIVE.get()


def powershell_news_window_instruction(ticker, as_of):
    """Run-owned tool limits only, never a default identity or source fact."""
    owner = _ACTIVE.get()
    if owner is None:
        return ''
    try:
        matches = _ticker(ticker) == owner.request.ticker and as_of == owner.request.end
    except (ValueError, TypeError):
        matches = False
    if not matches:
        return '新闻会话绑定与当前分析目标不匹配；不得替换股票或沿用另一会话数据。'
    return (f'PowerShell 新闻可用请求窗口：{owner.request.start} 至 {owner.request.end}；'
            f'股票：{owner.request.ticker}。get_news 必须使用此窗口或其子窗口。'
            '超窗请求不会补采；仅复用本会话单次采集，缺口不代表没有新闻。'
            '这是工具范围说明，不是新闻事实。')

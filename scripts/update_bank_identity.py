"""Explicit stock_basic metadata update, independent of analysis budgets."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import warnings
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]


def main(argv=None, *, return_result=False, session_clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--refresh', action='store_true', help='Explicitly confirm one six-part metadata update.')
    action.add_argument('--diagnose-schema', action='store_true',
                        help='Explicitly confirm at most one SSE/L request; no usable snapshot.')
    action.add_argument('--refresh-security', metavar='TS_CODE',
                        help='One exact CURRENT listed bank identity query; explicit SH/SZ (SS alias).')
    parser.add_argument('--use-project-env', action='store_true')
    args = parser.parse_args(argv)
    sys.path.insert(0, str(SOURCE))
    sys.path.insert(0, str(SOURCE / 'scripts'))
    from analyze_mainland_bounded import read_json, require, verify_release
    require(sys.flags.isolated and sys.dont_write_bytecode, 'python_flags')
    verify_release(read_json(SOURCE / 'config/bounded_mainland_release.json'))
    require(Path(sys.executable).resolve() == SOURCE / '.venv/Scripts/python.exe', 'python_identity')
    if not args.refresh and not args.diagnose_schema and args.refresh_security is None:
        print(json.dumps({'status': 'source_preflight_passed', 'acquisition': False}))
        return 0
    import ctypes
    require(not ctypes.windll.shell32.IsUserAnAdmin(), 'non_admin_required')
    require(sys.stdin.isatty(), 'interactive_terminal_required')
    exact_symbol = None
    if args.refresh_security is not None:
        from tradingagents.dataflows.bank_identity_snapshot import exact_query
        exact_symbol = exact_query(args.refresh_security)['params']['ts_code']
        print('ONE exact CURRENT bank identity query for ' + exact_symbol + ': <=1 POST to '
              'https://api.waditu.com/dataapi/stock_basic. No retry/redirect/model. '
              '30s I/O, 240s cooperative total, existing <=40/min ceiling. '
              'Only Tushare Token; positive exact identity only, partial universe, no historical PIT. '
              'New pinned identity evidence and necessary coordinator writes; old records retained. '
              'Empty/ambiguous/paging-unknown response stops; no automatic analysis.')
        confirmation = 'UPDATE_BANK_IDENTITY:' + exact_symbol
    elif args.diagnose_schema:
        print('ONE response-structure diagnostic: SSE/L only, <=1 POST to '
              'https://api.waditu.com/dataapi/stock_basic. No retry/redirect/model. '
              '30s I/O, 240s cooperative total, existing <=40/min ceiling. '
              'Only Tushare Token is sent. New sanitized diagnostic receipts and necessary '
              'existing coordinator writes; no raw response or usable snapshot, no READY, '
              'no analysis or old state reset.')
        confirmation = 'DIAGNOSE_BANK_SCHEMA'
    else:
        print('ONE CURRENT bank metadata update: SSE/SZSE x L/D/P, <=6 POST to '
          'https://api.waditu.com/dataapi/stock_basic. No retry/redirect/model. '
          '30s I/O, 240s cooperative total, existing <=40/min ceiling. '
          'This standalone operation has its own limit. Only Tushare Token is sent. '
          'New metadata snapshot and necessary existing coordinator writes; no old data reset. '
              '24h freshness, CURRENT metadata is not historical PIT or a trading conclusion.')
        confirmation = 'UPDATE_BANK_IDENTITIES'
    require(input('Type ' + confirmation + ' to send this bounded request: ') == confirmation,
            'not_confirmed')
    os.environ.update(PYTHON_DOTENV_DISABLED='1', LANGCHAIN_TRACING_V2='false',
        LANGCHAIN_TRACING='false', LANGSMITH_TRACING='false', OTEL_SDK_DISABLED='true')
    if args.use_project_env:
        from project_credentials import read_project_credentials
        keys = read_project_credentials(SOURCE, names=('TUSHARE_TOKEN',))
        token = keys.pop('TUSHARE_TOKEN')
        keys.clear()
    else:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = getpass.getpass('Tushare Token only (hidden, not saved): ')
    try:
        from tradingagents.dataflows.bank_identity_acquisition import CaptureClock, update
        clock = CaptureClock()
        if session_clock is not None:
            clock.end = min(clock.end, session_clock.deadline_monotonic)
            clock.end_epoch = min(clock.end_epoch, session_clock.effective_deadline())
            clock.remaining()
        result = update(SOURCE, token, clock=clock, diagnose_schema=args.diagnose_schema,
                        exact_symbol=exact_symbol)
    finally:
        token = None
    print(json.dumps(result))
    expected = ('exact_positive' if exact_symbol is not None else
                'diagnostic_complete' if args.diagnose_schema else 'complete')
    if return_result:
        require(exact_symbol is not None and result['status'] == expected, 'bank_refresh_failed')
        return result
    return 0 if result['status'] == expected else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'status': 'stopped', 'reason': 'bank_update_entry_error', 'resume': False}))
        raise SystemExit(1) from None

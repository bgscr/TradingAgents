from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode,expected,reads', [
    ('authorized', 'read', 1), ('hidden', 'denied', 0), ('reuse', 'denied', 0),
    ('direct', 'denied', 0), ('write', 'denied', 0), ('wrong_file', 'denied', 0),
    ('repeat', 'denied', 1),
])
def test_real_child_guard_allows_only_one_fixed_helper_read(tmp_path, mode, expected, reads):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from diagnostic_runtime import safe_environment
    source, output = tmp_path / 'project', tmp_path / 'output'
    (source / 'scripts').mkdir(parents=True)
    output.mkdir()
    shutil.copyfile(ROOT / 'scripts/project_credentials.py', source / 'scripts/project_credentials.py')
    for name in ('diagnostic_runtime.py', 'financial_network_guard.py'):
        shutil.copyfile(ROOT / 'scripts' / name, source / 'scripts' / name)
    original = 'TUSHARE_TOKEN=synthetic-tushare-only\nDEEPSEEK_API_KEY=synthetic-deepseek-only\n'
    (source / '.env').write_text(original)
    (source / 'nested').mkdir()
    (source / 'nested/.env').write_text(original)
    child = subprocess.run([sys.executable, '-I', '-S', '-B',
                            str(ROOT / 'tests/project_credentials_child.py'),
                            str(ROOT), str(source), str(output), mode],
                           env=safe_environment(output), capture_output=True, text=True,
                           check=False, timeout=30)
    if expected == 'read':
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout)
        assert result['status'] == expected and result['expected_synthetic_value'] is True
        assert result['counts'].get('project_env_reads', 0) == reads
        assert result['counts']['network'] == 0 and result['counts']['subprocess'] == 0
    else:
        assert child.returncode == 74, child.stderr  # Production guard's hard stop, not swallowed.
        receipt = json.loads((output / ('reuse-initialization.json' if mode == 'reuse'
                                      else 'live-initialization.json')).read_text())
        assert receipt['reason'] == 'unexpected_acceptance_denial'
        assert receipt['first_unexpected_denial']['event_category'] == 'open'
        assert receipt['counts']['socket_creations'] == 0
        if mode == 'repeat':
            assert json.loads(child.stdout) == {'first_read_verified': True, 'authorized_read_count': 1}
    assert 'synthetic-tushare-only' not in child.stdout + child.stderr
    assert 'synthetic-deepseek-only' not in child.stdout + child.stderr
    assert (source / '.env').read_text() == original


def test_project_env_guard_preserves_isolated_interpreter_precondition(tmp_path):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import diagnostic_runtime
    # Flag validation is also exercised by genuine -I -S -B children above.
    # Reject before inspecting values; the interpreter precondition remains intact.
    with pytest.raises(RuntimeError, match='diagnostics require'):
        diagnostic_runtime.install(tmp_path, ROOT, project_env_credentials=True)

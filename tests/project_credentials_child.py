"""Zero-network child exercising the installed-style credential audit boundary."""
import json
import os
import sys
from pathlib import Path

candidate, source, output = map(Path, sys.argv[1:4])
mode = sys.argv[4]

# Protect this isolated synthetic child even before the production guard loads.
def offline_audit(event, args):
    if event in {'socket.connect', 'socket.bind', 'socket.sendto', 'socket.getaddrinfo',
                 'subprocess.Popen', 'os.system'}:
        raise RuntimeError('offline_child_transport_forbidden')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).absolute()
        if path.name.startswith('.env') and not path.is_relative_to(source):
            raise RuntimeError('real_credential_file_forbidden')

sys.addaudithook(offline_audit)
sys.path[:0] = [str(source / 'scripts'), str(candidate / 'scripts')]
# Install the boundary before reading product inputs.
from diagnostic_runtime import DeniedAccess, install  # noqa: E402
from financial_network_guard import FinancialNetworkGuard  # noqa: E402


class SyntheticStorage:
    def connection_factory(self, original):
        return original


allowed = mode not in {'hidden', 'reuse'}
installed = install(output, source, offline=mode == 'reuse',
                    network_guard=FinancialNetworkGuard('https://api.waditu.com/dataapi'),
                    shared_storage=SyntheticStorage(),
                    acceptance_initialization='reuse' if mode == 'reuse' else 'live',
                    project_env_credentials=allowed)
counts = installed[0]
# Load the reader only under the real guard.
from project_credentials import read_project_credentials  # noqa: E402

checks = {}
try:
    if mode == 'direct':
        (source / '.env').read_text()
    elif mode == 'write':
        (source / '.env').open('w')
    elif mode == 'wrong_file':
        read_project_credentials(source / 'nested', names=('TUSHARE_TOKEN',))
    else:
        values = read_project_credentials(source, names=('TUSHARE_TOKEN',))
        checks['expected_synthetic_value'] = values == {'TUSHARE_TOKEN': 'synthetic-tushare-only'}
        values.clear()
        if mode == 'repeat':
            print(json.dumps({'first_read_verified': checks['expected_synthetic_value'],
                              'authorized_read_count': counts['project_env_reads']}), flush=True)
            read_project_credentials(source, names=('TUSHARE_TOKEN',))
    checks['status'] = 'read'
except DeniedAccess:
    checks['status'] = 'denied'
checks['counts'] = counts
print(json.dumps(checks))

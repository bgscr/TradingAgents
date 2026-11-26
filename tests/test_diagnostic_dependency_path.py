"""Local path checks use deliberately invalid payloads and never open a socket."""
import os
import sys

import pytest

from scripts.diagnostic_runtime import Urllib3LoopbackProbe


@pytest.mark.skipif(sys.version_info[:3] != (3, 13, 7), reason="probe pins CPython 3.13.7")
def test_probe_rejects_linked_dependency_root_before_payload_check(tmp_path):
    dependencies = tmp_path / "dependencies"
    package = dependencies / "urllib3"
    (package / "util").mkdir(parents=True)
    (package / "util/connection.py").write_bytes(b"# synthetic")
    (package / "_version.py").write_bytes(b"# synthetic")
    alias = tmp_path / "alias"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(dependencies), str(alias))
    else:
        alias.symlink_to(dependencies, target_is_directory=True)
    with pytest.raises(RuntimeError, match="^probe_policy_linked_implementation$"):
        Urllib3LoopbackProbe(tmp_path, alias)
    with pytest.raises(RuntimeError, match="^probe_policy_implementation_mismatch$"):
        Urllib3LoopbackProbe(tmp_path, dependencies)

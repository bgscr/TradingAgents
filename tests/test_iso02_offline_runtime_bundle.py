from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from packaging.utils import parse_wheel_filename

from scripts.build_iso02_offline_bundle import validate_portable_git_extraction
from tradingagents.offline_runtime_bundle import (
    OfflineBundleContractError,
    OfflineBundleFailureReason,
    OfflineBundleSourceBinding,
    OfflineRuntimeBundleBuildRequest,
    ProvisioningCommandRole,
    build_offline_install_plan,
    build_offline_runtime_bundle,
    execute_offline_provisioning,
    verify_offline_runtime_bundle,
)
from tradingagents.windows_sandbox_runner import (
    ProtectedObjectKind,
    ProtectedRootBinding,
    ProtectedRootRole,
    RunnerConstructionRequest,
    SandboxContractError,
    SandboxExecutionMode,
    SandboxFailureReason,
    SandboxPlatformReport,
    WindowsSandboxRunnerService,
    stage_reviewed_head_objects,
    stage_reviewed_source,
)


def _write_wheel(
    wheelhouse: Path,
    *,
    distribution: str,
    version: str,
    requires: tuple[str, ...] = (),
    python_tag: str = "py3",
    abi_tag: str = "none",
    platform_tag: str = "any",
    module_content: str = "",
    metadata_version: str | None = None,
) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    filename = f"{distribution}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl"
    path = wheelhouse / filename
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {metadata_version or version}",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
        "",
    ]
    dist_info = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        members = {
            f"{distribution}/__init__.py": module_content,
            f"{dist_info}/METADATA": "\n".join(metadata),
            f"{dist_info}/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: iso-02-test\n"
                f"Root-Is-Purelib: true\nTag: {python_tag}-{abi_tag}-{platform_tag}\n"
            ),
        }
        for member, content in members.items():
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, content)
    return path


def _source_binding(*, commit: str = "a" * 40) -> OfflineBundleSourceBinding:
    return OfflineBundleSourceBinding(
        contract_version="windows-sandbox-reviewed-source-v1",
        commit_id=commit,
        tree_hash="b" * 40,
        source_input_identity=(
            f"windows-sandbox-reviewed-source:v1:sha256:{'c' * 64}"
        ),
        source_manifest_identity=(
            f"windows-sandbox-source-manifest:v1:sha256:{'d' * 64}"
        ),
        source_archive_sha256="e" * 64,
    )


def _pe_fixture(*, machine: int = 0x8664) -> bytes:
    content = bytearray(512)
    content[:2] = b"MZ"
    content[60:64] = (128).to_bytes(4, "little")
    content[128:132] = b"PE\0\0"
    content[132:134] = machine.to_bytes(2, "little")
    return bytes(content)


def _bundle_inputs(
    root: Path,
    *,
    project_dependency: str = "demo==1.0",
    reverse_wheels: bool = False,
) -> dict[str, object]:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    python_installer = inputs / "python-3.13.7-amd64.exe"
    python_installer.write_bytes(b"reviewed-python-installer")
    bootstrap = inputs / "install-runtime.ps1"
    bootstrap.write_text("param()\n", encoding="utf-8")
    verifier = inputs / "verify_bundle.py"
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    portable_git = inputs / "MinGit"
    (portable_git / "cmd").mkdir(parents=True)
    (portable_git / "cmd" / "git.exe").write_bytes(b"reviewed-portable-git")
    (portable_git / "LICENSE.txt").write_text("Git license\n", encoding="utf-8")
    portable_git_archive = inputs / "MinGit-2.45.1-64-bit.zip"
    with zipfile.ZipFile(portable_git_archive, "w") as archive:
        for relative_path in ("LICENSE.txt", "cmd/git.exe"):
            info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, (portable_git / relative_path).read_bytes())
    wheelhouse = inputs / "wheelhouse"
    wheel_specs = (
        ("demo", "1.0", ("dependency==2.0",)),
        ("dependency", "2.0", ()),
        ("pip", "25.2", ()),
        ("wheel", "0.46.3", ()),
    )
    for distribution, version, requires in (
        reversed(wheel_specs) if reverse_wheels else wheel_specs
    ):
        _write_wheel(
            wheelhouse,
            distribution=distribution,
            version=version,
            requires=requires,
        )
    project_metadata = inputs / "pyproject.toml"
    project_metadata.write_text(
        "[project]\n"
        "name='fixture'\n"
        f"dependencies=['{project_dependency}']\n"
        "[project.optional-dependencies]\n"
        "dev=[]\n"
        "pit=[]\n",
        encoding="utf-8",
    )
    artifact_lock = inputs / "iso02-artifact-lock.json"
    python_content = python_installer.read_bytes()
    archive_content = portable_git_archive.read_bytes()
    artifact_lock.write_text(
        json.dumps(
            {
                "contract_version": "windows-offline-runtime-artifact-lock-v1",
                "portable_git": {
                    "archive_byte_length": len(archive_content),
                    "archive_filename": portable_git_archive.name,
                    "archive_sha256": sha256(archive_content).hexdigest(),
                    "required": True,
                    "source_url": "https://example.invalid/MinGit.zip",
                    "version": "2.45.1.windows.1",
                },
                "python": {
                    "architecture": "AMD64",
                    "artifact_byte_length": len(python_content),
                    "artifact_filename": python_installer.name,
                    "artifact_sha256": sha256(python_content).hexdigest(),
                    "implementation": "CPython",
                    "source_url": "https://example.invalid/python.exe",
                    "version": "3.13.7",
                },
            },
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    expected_dependency_lock = inputs / "requirements-win-cp313.txt"
    reviewed_source_manifest = inputs / "source-manifest.json"
    return {
        "python_installer": python_installer,
        "bootstrap_script": bootstrap,
        "verifier_script": verifier,
        "portable_git_root": portable_git,
        "portable_git_archive": portable_git_archive,
        "wheelhouse": wheelhouse,
        "project_metadata": project_metadata,
        "runtime_artifact_lock": artifact_lock,
        "expected_dependency_lock": expected_dependency_lock,
        "reviewed_source_manifest": reviewed_source_manifest,
    }


def _refresh_reviewed_request(
    request: OfflineRuntimeBundleBuildRequest,
    *,
    refresh_dependency_lock: bool = True,
) -> OfflineRuntimeBundleBuildRequest:
    if refresh_dependency_lock:
        wheel_rows = []
        for wheel in request.wheelhouse.glob("*.whl"):
            distribution, version, _build, _tags = parse_wheel_filename(wheel.name)
            wheel_rows.append(
                (
                    str(distribution),
                    f"{distribution}=={version} --hash=sha256:"
                    f"{sha256(wheel.read_bytes()).hexdigest()}",
                )
            )
        request.expected_dependency_lock.write_bytes(
            (
                "\n".join(line for _distribution, line in sorted(wheel_rows)) + "\n"
            ).encode("utf-8")
        )
    tracked_inputs = {
        "pyproject.toml": request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": (
            request.runtime_artifact_lock
        ),
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": (
            request.expected_dependency_lock
        ),
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": (
            request.bootstrap_script
        ),
        "scripts/windows_sandbox/verify_offline_bundle.py": request.verifier_script,
    }
    entries = []
    for relative_path, path in sorted(tracked_inputs.items()):
        content = path.read_bytes()
        entries.append(
            {
                "byte_length": len(content),
                "git_mode": "100644",
                "git_object_id": sha256(content).hexdigest()[:40],
                "relative_path": relative_path,
                "sha256": sha256(content).hexdigest(),
            }
        )
    manifest_payload = {
        "commit_id": request.source.commit_id,
        "contract_version": request.source.contract_version,
        "entries": entries,
        "git_object_format": "sha1",
        "run_live_environment": [],
        "tree_hash": request.source.tree_hash,
    }
    manifest_identity = (
        "windows-sandbox-source-manifest:v1:sha256:"
        + sha256(
            json.dumps(
                manifest_payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    source = request.source.model_copy(
        update={"source_manifest_identity": manifest_identity}
    )
    request.reviewed_source_manifest.write_text(
        json.dumps(
            {**manifest_payload, "source_manifest_identity": manifest_identity},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return replace(request, source=source)


def _build_request(
    root: Path,
    *,
    source: OfflineBundleSourceBinding | None = None,
    project_dependency: str = "demo==1.0",
    reverse_wheels: bool = False,
) -> OfflineRuntimeBundleBuildRequest:
    inputs = _bundle_inputs(
        root,
        project_dependency=project_dependency,
        reverse_wheels=reverse_wheels,
    )
    project_metadata = inputs["project_metadata"].read_bytes()
    request = OfflineRuntimeBundleBuildRequest(
        destination=root / "bundle",
        source=source or _source_binding(),
        python_installer=inputs["python_installer"],
        python_version="3.13.7",
        windows_architecture="AMD64",
        wheelhouse=inputs["wheelhouse"],
        dependency_roots=("demo==1.0", "pip==25.2", "wheel==0.46.3")
        if project_dependency == "demo==1.0"
        else tuple(sorted((project_dependency, "pip==25.2", "wheel==0.46.3"))),
        project_metadata_sha256=sha256(project_metadata).hexdigest(),
        bootstrap_script=inputs["bootstrap_script"],
        verifier_script=inputs["verifier_script"],
        portable_git_root=inputs["portable_git_root"],
        reviewed_source_manifest=inputs["reviewed_source_manifest"],
        runtime_artifact_lock=inputs["runtime_artifact_lock"],
        project_metadata=inputs["project_metadata"],
        portable_git_archive=inputs["portable_git_archive"],
        expected_dependency_lock=inputs["expected_dependency_lock"],
    )
    return _refresh_reviewed_request(request)


def test_iso_02_locked_portable_git_archive_must_match_distributed_tree(
    tmp_path: Path,
) -> None:
    portable_git = tmp_path / "MinGit"
    (portable_git / "cmd").mkdir(parents=True)
    git_executable = portable_git / "cmd" / "git.exe"
    git_executable.write_bytes(b"reviewed-portable-git")
    archive_path = tmp_path / "MinGit.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(git_executable, "cmd/git.exe")

    validate_portable_git_extraction(archive_path, portable_git)
    git_executable.write_bytes(b"unreviewed-portable-git")

    with pytest.raises(RuntimeError, match="reviewed_portable_git_extraction_mismatch"):
        validate_portable_git_extraction(archive_path, portable_git)


def test_iso_02_bundle_identity_is_path_and_enumeration_independent_and_binds_source(
    tmp_path: Path,
) -> None:
    first = build_offline_runtime_bundle(_build_request(tmp_path / "first"))
    second = build_offline_runtime_bundle(
        _build_request(tmp_path / "unrelated-host-path", reverse_wheels=True)
    )
    changed_source = build_offline_runtime_bundle(
        _build_request(
            tmp_path / "changed-source",
            source=_source_binding(commit="f" * 40),
        )
    )

    assert first.manifest.contract_version == "windows-offline-runtime-bundle-v1"
    assert first.manifest.bundle_identity == second.manifest.bundle_identity
    assert first.manifest.bundle_identity != changed_source.manifest.bundle_identity
    assert str(tmp_path) not in first.manifest.canonical_json()
    assert [entry.relative_path for entry in first.manifest.files] == sorted(
        (entry.relative_path for entry in first.manifest.files),
        key=str.casefold,
    )


def test_iso_02_reviewed_support_bytes_cannot_be_substituted_under_source_identity(
    tmp_path: Path,
) -> None:
    request = _build_request(tmp_path / "substituted-support")
    request.bootstrap_script.write_text("param()\n# substituted\n", encoding="utf-8")

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH


def test_iso_02_python_bytes_cannot_be_substituted_under_artifact_lock(
    tmp_path: Path,
) -> None:
    request = _build_request(tmp_path / "substituted-python")
    request.python_installer.write_bytes(b"substituted-python-installer")

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH


def test_iso_02_portable_git_tree_must_match_locked_archive(tmp_path: Path) -> None:
    request = _build_request(tmp_path / "substituted-portable-git")
    (request.portable_git_root / "cmd/git.exe").write_bytes(
        b"substituted-portable-git"
    )

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH


def test_iso_02_complete_bundle_verifies_all_files_and_dependency_closure(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "complete"))

    verified = verify_offline_runtime_bundle(
        built.root,
        expected_source=built.manifest.source,
    )

    assert verified.bundle_identity == built.manifest.bundle_identity
    assert {wheel.distribution for wheel in verified.wheelhouse_inventory} == {
        "demo",
        "dependency",
        "pip",
        "wheel",
    }


def test_iso_02_changed_wheel_bytes_change_bundle_identity(tmp_path: Path) -> None:
    first = build_offline_runtime_bundle(_build_request(tmp_path / "original-wheel"))
    request = _build_request(tmp_path / "changed-wheel")
    request.wheelhouse.joinpath("demo-1.0-py3-none-any.whl").unlink()
    _write_wheel(
        request.wheelhouse,
        distribution="demo",
        version="1.0",
        requires=("dependency==2.0",),
        module_content="CHANGED = True\n",
    )
    request = _refresh_reviewed_request(request)

    changed = build_offline_runtime_bundle(request)

    assert first.manifest.bundle_identity != changed.manifest.bundle_identity
    assert first.manifest.wheelhouse_inventory_identity != (
        changed.manifest.wheelhouse_inventory_identity
    )


def test_iso_02_missing_transitive_dependency_fails_closed(tmp_path: Path) -> None:
    request = _build_request(tmp_path / "missing-transitive")
    (request.wheelhouse / "dependency-2.0-py3-none-any.whl").unlink()

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.MISSING_TRANSITIVE_DEPENDENCY


@pytest.mark.parametrize("late_expansion", (False, True))
def test_iso_02_missing_extra_dependency_fails_closed_even_after_late_expansion(
    tmp_path: Path,
    late_expansion: bool,
) -> None:
    request = _build_request(tmp_path / f"missing-extra-{late_expansion}")
    for name in ("demo-1.0-py3-none-any.whl", "dependency-2.0-py3-none-any.whl"):
        (request.wheelhouse / name).unlink()
    if late_expansion:
        demo_requires = ("base==1.0", "extra-activator==1.0")
        _write_wheel(
            request.wheelhouse,
            distribution="extra_activator",
            version="1.0",
            requires=("base[feature]==1.0",),
        )
    else:
        demo_requires = ("base[feature]==1.0",)
    _write_wheel(
        request.wheelhouse,
        distribution="demo",
        version="1.0",
        requires=demo_requires,
    )
    _write_wheel(
        request.wheelhouse,
        distribution="base",
        version="1.0",
        requires=('extra-dependency==1.0; extra == "feature"',),
    )

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.MISSING_TRANSITIVE_DEPENDENCY


def test_iso_02_duplicate_conflicting_distribution_fails_closed(tmp_path: Path) -> None:
    request = _build_request(tmp_path / "duplicate")
    _write_wheel(request.wheelhouse, distribution="demo", version="9.0")

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.DUPLICATE_DISTRIBUTION


@pytest.mark.parametrize(
    ("python_tag", "abi_tag", "platform_tag", "reason"),
    (
        ("cp312", "cp312", "win_amd64", OfflineBundleFailureReason.WRONG_PYTHON_ABI),
        ("cp313", "cp313", "win_arm64", OfflineBundleFailureReason.WRONG_ARCHITECTURE),
        ("cp313", "cp313", "manylinux_2_28_x86_64", OfflineBundleFailureReason.WRONG_PLATFORM),
    ),
)
def test_iso_02_incompatible_wheel_fails_closed(
    tmp_path: Path,
    python_tag: str,
    abi_tag: str,
    platform_tag: str,
    reason: OfflineBundleFailureReason,
) -> None:
    request = _build_request(tmp_path / reason.value)
    request.wheelhouse.joinpath("demo-1.0-py3-none-any.whl").unlink()
    _write_wheel(
        request.wheelhouse,
        distribution="demo",
        version="1.0",
        requires=("dependency==2.0",),
        python_tag=python_tag,
        abi_tag=abi_tag,
        platform_tag=platform_tag,
    )

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(replace(request, destination=request.destination))

    assert raised.value.reason is reason


def test_iso_02_tamper_fails_before_any_installer_execution(tmp_path: Path) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "tamper"))
    installer = built.root / built.manifest.python_installer_relative_path
    original = installer.read_bytes()
    installer.write_bytes(b"X" + original[1:])
    calls: list[tuple[str, ...]] = []

    with pytest.raises(OfflineBundleContractError) as raised:
        execute_offline_provisioning(
            built.root,
            expected_source=built.manifest.source,
            executor=lambda argv, _environment: calls.append(argv) or 0,
        )

    assert raised.value.reason is OfflineBundleFailureReason.FILE_HASH_MISMATCH
    assert calls == []


def test_iso_02_missing_wheel_fails_before_any_installer_execution(tmp_path: Path) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "missing-wheel"))
    wheel = built.root / "wheelhouse" / built.manifest.wheelhouse_inventory[0].filename
    wheel.unlink()
    calls: list[tuple[str, ...]] = []

    with pytest.raises(OfflineBundleContractError) as raised:
        execute_offline_provisioning(
            built.root,
            expected_source=built.manifest.source,
            executor=lambda argv, _environment: calls.append(argv) or 0,
        )

    assert raised.value.reason is OfflineBundleFailureReason.FILE_MISSING
    assert calls == []


def test_iso_02_wrong_wheel_hash_fails_before_any_installer_execution(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "wrong-wheel-hash"))
    wheel = built.root / "wheelhouse" / built.manifest.wheelhouse_inventory[0].filename
    content = wheel.read_bytes()
    wheel.write_bytes(b"X" + content[1:])
    calls: list[tuple[str, ...]] = []

    with pytest.raises(OfflineBundleContractError) as raised:
        execute_offline_provisioning(
            built.root,
            expected_source=built.manifest.source,
            executor=lambda argv, _environment: calls.append(argv) or 0,
        )

    assert raised.value.reason is OfflineBundleFailureReason.FILE_HASH_MISMATCH
    assert calls == []


def test_iso_02_cross_artifact_identity_mismatch_fails_before_execution(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "identity-mismatch"))
    manifest_path = built.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["portable_git_identity"] = (
        f"windows-offline-portable-git:v1:sha256:{'0' * 64}"
    )
    payload = {key: value for key, value in manifest.items() if key != "bundle_identity"}
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest["bundle_identity"] = (
        "windows-offline-runtime-bundle:v1:sha256:" + sha256(canonical).hexdigest()
    )
    manifest_path.write_text(
        json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    with pytest.raises(OfflineBundleContractError) as raised:
        execute_offline_provisioning(
            built.root,
            expected_source=built.manifest.source,
            executor=lambda argv, _environment: calls.append(argv) or 0,
        )

    assert raised.value.reason is OfflineBundleFailureReason.MANIFEST_IDENTITY_MISMATCH
    assert calls == []


def test_iso_02_source_mismatch_and_extra_file_fail_closed(tmp_path: Path) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "source-and-extra"))

    with pytest.raises(OfflineBundleContractError) as source_error:
        verify_offline_runtime_bundle(
            built.root,
            expected_source=built.manifest.source.model_copy(
                update={"commit_id": "f" * 40}
            ),
        )
    assert source_error.value.reason is OfflineBundleFailureReason.SOURCE_BINDING_MISMATCH

    (built.root / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(OfflineBundleContractError) as extra_error:
        verify_offline_runtime_bundle(
            built.root,
            expected_source=built.manifest.source,
        )
    assert extra_error.value.reason is OfflineBundleFailureReason.FILE_EXTRA


def test_iso_02_install_plan_is_guest_local_hash_locked_and_has_no_network_fallback(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "install-plan"))

    plan = build_offline_install_plan(built.manifest)
    by_role = {command.role: command for command in plan.commands}
    dependency = by_role[ProvisioningCommandRole.INSTALL_DEPENDENCIES]
    project = by_role[ProvisioningCommandRole.INSTALL_PROJECT]

    assert "--no-index" in dependency.argv
    assert "--isolated" in dependency.argv
    assert "--require-hashes" in dependency.argv
    assert "--find-links" in dependency.argv
    assert r"C:\QCF17\Input\Runtime\wheelhouse" in dependency.argv
    assert "--no-index" in project.argv
    assert "--isolated" in project.argv
    assert "--no-deps" in project.argv
    assert "--no-build-isolation" in project.argv
    assert "--expected-source-input-identity" in by_role[
        ProvisioningCommandRole.VERIFY_WHEELHOUSE
    ].argv
    assert built.manifest.source.source_input_identity in by_role[
        ProvisioningCommandRole.VERIFY_WHEELHOUSE
    ].argv
    assert project.argv[-1] == r"C:\QCF17\Work\Repository"
    assert all("http://" not in item and "https://" not in item for command in plan.commands for item in command.argv)
    assert all(not item.casefold().startswith(str(tmp_path).casefold()) for command in plan.commands for item in command.argv)
    assert plan.environment == (
        ("PIP_CONFIG_FILE", "NUL"),
        ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
        ("PIP_NO_INDEX", "1"),
        ("PYTHONNOUSERSITE", "1"),
    )


def test_iso_02_hash_locked_requirements_are_deterministic_and_cover_every_wheel(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "requirements"))
    requirements = (
        built.root / built.manifest.dependency_lock_relative_path
    ).read_text(encoding="utf-8")

    assert requirements.splitlines() == sorted(requirements.splitlines())
    assert len(requirements.splitlines()) == len(built.manifest.wheelhouse_inventory)
    for wheel in built.manifest.wheelhouse_inventory:
        assert f"{wheel.distribution}=={wheel.version} --hash=sha256:{wheel.sha256}" in requirements


def test_iso_02_reviewed_dependency_lock_mismatch_fails_closed(tmp_path: Path) -> None:
    request = _build_request(tmp_path / "reviewed-lock")
    wrong_lock = tmp_path / "wrong-lock.txt"
    wrong_lock.write_text("demo==1.0 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")

    with pytest.raises(OfflineBundleContractError) as raised:
        mismatched = _refresh_reviewed_request(
            replace(request, expected_dependency_lock=wrong_lock),
            refresh_dependency_lock=False,
        )
        build_offline_runtime_bundle(mismatched)

    assert raised.value.reason is OfflineBundleFailureReason.DEPENDENCY_LOCK_MISMATCH


def test_iso_02_provisioning_evidence_is_bounded_secret_free_and_network_sentinel_clean(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "safe-evidence"))
    calls: list[tuple[str, ...]] = []

    def sentinel(
        argv: tuple[str, ...], environment: tuple[tuple[str, str], ...]
    ) -> int:
        assert dict(environment)["PIP_NO_INDEX"] == "1"
        assert not any("http://" in value or "https://" in value for value in argv)
        assert not any(value.casefold() in {"curl", "wget", "invoke-webrequest"} for value in argv)
        calls.append(argv)
        return 0

    evidence = execute_offline_provisioning(
        built.root,
        expected_source=built.manifest.source,
        executor=sentinel,
    )
    serialized = evidence.model_dump_json()

    assert len(calls) == len(ProvisioningCommandRole)
    assert evidence.network_used is False
    assert evidence.host_python_used is False
    assert len(serialized.encode("utf-8")) < 4096
    assert str(tmp_path) not in serialized
    assert "token" not in serialized.casefold()


@pytest.mark.parametrize(
    ("relative_path", "content", "reason"),
    (
        (
            ".env",
            "PROVIDER_" + "TOKEN=not-a-placeholder",
            OfflineBundleFailureReason.FORBIDDEN_INPUT,
        ),
        (
            ".env.enterprise",
            "MODEL_" + "API_KEY=not-a-placeholder",
            OfflineBundleFailureReason.FORBIDDEN_INPUT,
        ),
        (
            "pip.ini",
            "index-url=https://" + "operator:package-token@example.invalid/simple",
            OfflineBundleFailureReason.FORBIDDEN_INPUT,
        ),
        (
            "support.txt",
            "https://" + "operator:package-token@example.invalid/simple",
            OfflineBundleFailureReason.CREDENTIAL_INPUT,
        ),
        (
            "host-path.txt",
            r"C:\Users\operator\private\bundle-input",
            OfflineBundleFailureReason.FORBIDDEN_INPUT,
        ),
        ("production.sqlite3", "runtime state", OfflineBundleFailureReason.FORBIDDEN_INPUT),
    ),
)
def test_iso_02_secret_and_production_state_inputs_are_rejected_safely(
    tmp_path: Path,
    relative_path: str,
    content: str,
    reason: OfflineBundleFailureReason,
) -> None:
    request = _build_request(tmp_path / relative_path.replace(".", "_"))
    canary = request.portable_git_root / relative_path
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_text(content, encoding="utf-8")

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is reason
    assert content not in str(raised.value)


@pytest.mark.parametrize(
    ("member", "content", "reason"),
    (
        (
            "demo/secret.py",
            "API_" + "TOKEN = 's" + "k-abcdefghijklmnopqrstuvwxyz'\n",
            OfflineBundleFailureReason.CREDENTIAL_INPUT,
        ),
        (
            "demo/credentialed_url.py",
            "INDEX = 'https://" + "operator:package-token@example.invalid/simple'\n",
            OfflineBundleFailureReason.CREDENTIAL_INPUT,
        ),
        (
            "demo/host_path.py",
            r"BUILD_ROOT = 'C:\Users\operator\private\wheel-build'",
            OfflineBundleFailureReason.FORBIDDEN_INPUT,
        ),
        (
            "demo/./normalized-collision.py",
            "VALUE = 1\n",
            OfflineBundleFailureReason.WHEEL_INVALID,
        ),
    ),
)
def test_iso_02_unsafe_wheel_archive_members_fail_closed(
    tmp_path: Path,
    member: str,
    content: str,
    reason: OfflineBundleFailureReason,
) -> None:
    request = _build_request(tmp_path / reason.value)
    wheel = request.wheelhouse / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(member, content)

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is reason
    assert content not in str(raised.value)


def test_iso_02_malformed_wheel_metadata_is_rejected_without_value_leakage(
    tmp_path: Path,
) -> None:
    request = _build_request(tmp_path / "malformed-wheel-metadata")
    wheel = request.wheelhouse / "demo-1.0-py3-none-any.whl"
    canary = "actualSecret123"
    wheel.unlink()
    _write_wheel(
        request.wheelhouse,
        distribution="demo",
        version="1.0",
        requires=("dependency==2.0",),
        metadata_version=canary,
    )

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.WHEEL_INVALID
    assert canary not in str(raised.value)


def test_iso_02_native_wheel_members_are_individually_inventoried(
    tmp_path: Path,
) -> None:
    request = _build_request(tmp_path / "native-wheel-inventory")
    wheel = request.wheelhouse / "demo-1.0-py3-none-any.whl"
    executable = _pe_fixture()
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("demo/tool.exe", executable)

    request = _refresh_reviewed_request(request)
    built = build_offline_runtime_bundle(request)
    demo = next(
        item for item in built.manifest.wheelhouse_inventory if item.distribution == "demo"
    )

    assert len(demo.native_artifacts) == 1
    native = demo.native_artifacts[0]
    assert native.relative_path == "demo/tool.exe"
    assert native.byte_length == len(executable)
    assert native.sha256 == sha256(executable).hexdigest()
    assert native.artifact_type == "exe"
    assert native.machine_type == "AMD64"


@pytest.mark.parametrize(
    ("content", "reason"),
    (
        (b"MZnot-a-valid-pe", OfflineBundleFailureReason.WHEEL_INVALID),
        (_pe_fixture(machine=0xAA64), OfflineBundleFailureReason.WRONG_ARCHITECTURE),
    ),
)
def test_iso_02_native_wheel_member_must_be_valid_amd64_pe(
    tmp_path: Path,
    content: bytes,
    reason: OfflineBundleFailureReason,
) -> None:
    request = _build_request(tmp_path / reason.value)
    wheel = request.wheelhouse / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("demo/tool.exe", content)

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is reason


def test_iso_02_hidden_wheelhouse_input_is_rejected_safely(tmp_path: Path) -> None:
    request = _build_request(tmp_path / "hidden-wheelhouse-input")
    canary = "PIP_" + "INDEX_URL=https://" + "operator:secret@example.invalid/simple"
    (request.wheelhouse / ".env").write_text(canary, encoding="utf-8")

    with pytest.raises(OfflineBundleContractError) as raised:
        build_offline_runtime_bundle(request)

    assert raised.value.reason is OfflineBundleFailureReason.FORBIDDEN_INPUT
    assert canary not in str(raised.value)


def test_iso_02_manifest_contains_no_source_archive_or_host_runtime_material(
    tmp_path: Path,
) -> None:
    built = build_offline_runtime_bundle(_build_request(tmp_path / "closed-membership"))
    paths = {entry.relative_path.casefold() for entry in built.manifest.files}

    assert "reviewed-source.tar" not in paths
    assert all(".env" not in path for path in paths)
    assert all(not path.endswith((".db", ".sqlite", ".sqlite3")) for path in paths)
    assert all("venv" not in path for path in paths)


def test_iso_02_guest_verifier_accepts_complete_bundle_without_project_import(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    request = _build_request(
        tmp_path / "guest-verifier",
        project_dependency="alpha_z==1.0",
    )
    for name in ("demo-1.0-py3-none-any.whl", "dependency-2.0-py3-none-any.whl"):
        (request.wheelhouse / name).unlink()
    _write_wheel(
        request.wheelhouse,
        distribution="alpha_z",
        version="1.0",
        requires=("alpha0==2.0",),
    )
    _write_wheel(
        request.wheelhouse,
        distribution="alpha0",
        version="2.0",
    )
    request.verifier_script.write_bytes(
        (project_root / "scripts/windows_sandbox/verify_offline_bundle.py").read_bytes()
    )
    request = _refresh_reviewed_request(request)
    built = build_offline_runtime_bundle(request)

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(built.root / built.manifest.verifier_relative_path),
            "--bundle-root",
            str(built.root),
            "--expected-bundle-identity",
            built.manifest.bundle_identity,
            "--expected-source-input-identity",
            built.manifest.source.source_input_identity,
        ],
        check=False,
        capture_output=True,
        shell=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "windows-offline-python-verification-v1" in completed.stdout
    assert "tradingagents" not in completed.stderr


def test_iso_02_guest_bootstrap_verifies_before_installer_and_stops_before_acceptance() -> None:
    project_root = Path(__file__).parents[1]
    bootstrap = (
        project_root / "scripts/windows_sandbox/offline_bundle_bootstrap.ps1"
    ).read_text(encoding="utf-8")
    normalized = bootstrap.casefold()

    assert normalized.index("get-filehash") < normalized.index("& $pythoninstaller")
    for relative_path, role in (
        ("provenance/mingit-2.45.1-64-bit.zip", "portable_git_archive"),
        ("provenance/iso02-artifact-lock.json", "runtime_artifact_lock"),
        ("provenance/pyproject.toml", "project_metadata"),
        ("provenance/source-manifest.json", "reviewed_source_manifest"),
    ):
        assert f'"{relative_path}" = "{role}"' in normalized
    assert normalized.index("source_manifest_identity_mismatch") < normalized.index(
        "& $pythoninstaller"
    )
    assert normalized.index("python_artifact_lock_mismatch") < normalized.index(
        "& $pythoninstaller"
    )
    assert "--dry-run --no-index --require-hashes" in normalized
    assert "--no-index --no-deps --no-build-isolation" in normalized
    assert "*> $null" in normalized
    assert "4096" in normalized
    assert "pip_config_file" in normalized
    assert "--isolated" in normalized
    assert "assert-pinnedpath" in normalized
    for forbidden in ("ticket 06", "ticket06", "ticket 07", "ticket07", "qualified_v1"):
        assert forbidden not in normalized


class _AvailablePlatform:
    def probe(self) -> SandboxPlatformReport:
        return SandboxPlatformReport.available(protected_client_supported=True)

    def launch(self, _config_path: Path, *, timeout_seconds: int):  # pragma: no cover
        raise AssertionError(f"Sandbox launch is outside ISO-02: {timeout_seconds}")


def _git(repository: Path, *arguments: str) -> None:
    environment = {
        "GIT_AUTHOR_NAME": "ISO 02",
        "GIT_AUTHOR_EMAIL": "iso02@example.invalid",
        "GIT_COMMITTER_NAME": "ISO 02",
        "GIT_COMMITTER_EMAIL": "iso02@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        ["git", "-c", "core.hooksPath=NUL", *arguments],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        shell=False,
    )


def _runner_repository(root: Path) -> Path:
    project_root = Path(__file__).parents[1]
    repository = root / "reviewed-repository"
    scripts = repository / "scripts/windows_sandbox"
    scripts.mkdir(parents=True)
    for name in ("bootstrap.ps1", "verify_source.py"):
        shutil.copyfile(project_root / "scripts/windows_sandbox" / name, scripts / name)
    (repository / "README.md").write_text("reviewed source\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "reviewed source")
    return repository


def test_iso_02_reviewed_hash_lock_allows_package_name_ending_in_token_only(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "lock-repository"
    lock = repository / "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        "tiktoken==0.12.0 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "valid reviewed lock")

    staged = stage_reviewed_head_objects(repository, tmp_path / "valid-lock-stage")

    assert staged.identity.tracked_file_count == 1

    lock.write_text("tiktoken==0.12.0\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "invalid reviewed lock")
    with pytest.raises(SandboxContractError) as raised:
        stage_reviewed_head_objects(repository, tmp_path / "invalid-lock-stage")

    assert raised.value.reason is SandboxFailureReason.FORBIDDEN_INPUT


def test_iso_02_reviewed_hash_lock_rejects_non_version_credential_canary(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "credential-lock-repository"
    lock = repository / "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
    lock.parent.mkdir(parents=True)
    canary = "actualSecret123"
    lock.write_text(
        f"tiktoken=={canary} --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "credential canary in reviewed lock")

    with pytest.raises(SandboxContractError) as raised:
        stage_reviewed_head_objects(repository, tmp_path / "credential-lock-stage")

    assert raised.value.reason is SandboxFailureReason.FORBIDDEN_INPUT
    assert canary not in str(raised.value)


def _all_protected_bindings(root: Path) -> tuple[ProtectedRootBinding, ...]:
    bindings: list[ProtectedRootBinding] = []
    for role in ProtectedRootRole:
        if role in {
            ProtectedRootRole.PROVIDER_LEDGER,
            ProtectedRootRole.PROVIDER_LEDGER_WAL,
            ProtectedRootRole.PROVIDER_LEDGER_SHM,
            ProtectedRootRole.PAYLOAD_MUTATION_LEDGER,
            ProtectedRootRole.CALENDAR_DATABASE,
            ProtectedRootRole.CALENDAR_DATABASE_WAL,
            ProtectedRootRole.CALENDAR_DATABASE_SHM,
            ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE,
        }:
            path = root / "files" / f"{role.value}.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.FILE
        else:
            path = root / role.value
            path.mkdir(parents=True)
            kind = ProtectedObjectKind.DIRECTORY
        bindings.append(ProtectedRootBinding(role=role, path=path, object_kind=kind))
    return tuple(bindings)


def test_iso_02_sealed_bundle_satisfies_iso_01_read_only_mapping_contract(
    tmp_path: Path,
) -> None:
    repository = _runner_repository(tmp_path)
    bundle_request = _build_request(tmp_path / "runtime-bundle")
    reviewed_files = {
        "pyproject.toml": bundle_request.project_metadata,
        "scripts/windows_sandbox/iso02-artifact-lock.json": (
            bundle_request.runtime_artifact_lock
        ),
        "scripts/windows_sandbox/iso02-requirements-win-cp313.txt": (
            bundle_request.expected_dependency_lock
        ),
        "scripts/windows_sandbox/offline_bundle_bootstrap.ps1": (
            bundle_request.bootstrap_script
        ),
        "scripts/windows_sandbox/verify_offline_bundle.py": (
            bundle_request.verifier_script
        ),
    }
    for relative_path, input_path in reviewed_files.items():
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(input_path.read_bytes())
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "review ISO-02 bundle inputs")
    staged = stage_reviewed_source(repository, tmp_path / "staged-source")
    source = OfflineBundleSourceBinding(
        contract_version=staged.identity.contract_version,
        commit_id=staged.identity.commit_id,
        tree_hash=staged.identity.tree_hash,
        source_input_identity=staged.identity.source_input_identity,
        source_manifest_identity=staged.identity.source_manifest_identity,
        source_archive_sha256=staged.identity.archive_sha256,
    )
    built = build_offline_runtime_bundle(
        replace(
            bundle_request,
            source=source,
            reviewed_source_manifest=staged.manifest_path,
        )
    )
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    user_profile = tmp_path / "profile"
    user_profile.mkdir()
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()
    host_venv = tmp_path / "host-venv"
    host_venv.mkdir()
    request = RunnerConstructionRequest(
        repository_root=repository,
        runs_root=runs_root,
        run_id="iso02-sealed-runtime-0001",
        execution_mode=SandboxExecutionMode.BOUNDARY_QUALIFICATION,
        protected_root_bindings=_all_protected_bindings(tmp_path / "protected"),
        user_profile=user_profile,
        host_temp=host_temp,
        host_venv=host_venv,
        runtime_bundle_root=built.root,
    )

    contract = WindowsSandboxRunnerService(platform=_AvailablePlatform()).validate_only(request)

    runtime_mapping = next(
        mapping
        for mapping in contract.mapping_manifest.mappings
        if mapping.role.value == "runtime_bundle"
    )
    assert contract.runtime_bundle.state == "sealed"
    assert contract.runtime_bundle.bundle_manifest_identity == built.manifest.bundle_identity
    assert contract.runtime_bundle.bundle_manifest_sha256 == built.manifest_sha256
    assert runtime_mapping.read_only is True
    assert contract.paths.runtime_input == built.root.resolve()

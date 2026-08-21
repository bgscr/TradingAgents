from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tarfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from xml.etree import ElementTree

import pytest

from scripts import run_iso01_windows_sandbox
from tradingagents.windows_sandbox_runner import (
    BoundedResultSection,
    ProtectedObjectKind,
    ProtectedPathMetadata,
    ProtectedRootBinding,
    ProtectedRootManifest,
    ProtectedRootRole,
    ResultSectionStatus,
    RunnerConstructionRequest,
    RunnerIdentityMaterial,
    RuntimeBundleInputContract,
    SandboxContractError,
    SandboxExecutionMode,
    SandboxFailureReason,
    SandboxFeatureState,
    SandboxGuestResult,
    SandboxMappingBinding,
    SandboxMappingRole,
    SandboxPlatformReport,
    SandboxPlatformState,
    SandboxQualificationOutcome,
    WindowsSandboxPlatform,
    WindowsSandboxRunnerService,
    build_completion_marker,
    build_environment_policy,
    build_guest_layout,
    build_guest_result_template,
    build_mapping_manifest,
    build_protected_root_manifest,
    build_runner_identity,
    build_runtime_bundle_placeholder,
    default_protected_root_bindings,
    finalize_guest_result,
    render_wsb_configuration,
    stage_reviewed_source,
    validate_result_export,
    validate_wsb_configuration,
)


def _protected_bindings(root: Path) -> tuple[ProtectedRootBinding, ...]:
    root.mkdir(parents=True)
    files = root / "files"
    files.mkdir()
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
            path = files / f"{role.value}.bin"
            path.write_bytes(role.value.encode("ascii"))
            kind = ProtectedObjectKind.FILE
        else:
            path = root / role.value
            path.mkdir()
            kind = ProtectedObjectKind.DIRECTORY
        bindings.append(ProtectedRootBinding(role=role, path=path, object_kind=kind))
    return tuple(bindings)


def test_iso_01_protected_manifest_identity_is_canonical_and_order_independent(
    tmp_path: Path,
) -> None:
    bindings = _protected_bindings(tmp_path / "protected")

    forward = build_protected_root_manifest(bindings)
    reverse = build_protected_root_manifest(tuple(reversed(bindings)))

    assert forward.contract_version == "windows-sandbox-protected-roots-v3"
    assert forward.manifest_identity == reverse.manifest_identity
    assert forward.safe_projection() == reverse.safe_projection()
    assert tuple(entry.role for entry in forward.entries) == tuple(ProtectedRootRole)
    assert all(entry.path_identity.startswith("host-path:sha256:") for entry in forward.entries)
    assert str(tmp_path) not in forward.canonical_json()


def test_iso_01_default_manifest_protects_full_profile_and_verifier_roots(
    tmp_path: Path,
) -> None:
    user_profile = tmp_path / "profile"
    repository_root = tmp_path / "repository"
    user_profile.mkdir()
    repository_root.mkdir()

    bindings = default_protected_root_bindings(
        user_profile=user_profile,
        repository_root=repository_root,
        historical_qualification_runs_root=tmp_path / "historical-sandbox-runs",
    )
    by_role = {binding.role: binding for binding in bindings}

    assert by_role[ProtectedRootRole.APPLICATION_PROFILE].path == (user_profile / ".tradingagents")
    assert {
        ProtectedRootRole.PROVIDER_LEDGER,
        ProtectedRootRole.PROVIDER_LEDGER_WAL,
        ProtectedRootRole.PROVIDER_LEDGER_SHM,
        ProtectedRootRole.PROVIDER_LEDGER_PAYLOADS,
        ProtectedRootRole.PAYLOAD_MUTATION_LEDGER,
        ProtectedRootRole.CALENDAR_DATABASE,
        ProtectedRootRole.CALENDAR_DATABASE_WAL,
        ProtectedRootRole.CALENDAR_DATABASE_SHM,
        ProtectedRootRole.TICKET07_EVIDENCE,
        ProtectedRootRole.REPORTS_LOGS,
        ProtectedRootRole.CHECKPOINTS,
    }.issubset(by_role)
    history = user_profile / ".tradingagents" / "market_history"
    assert by_role[ProtectedRootRole.PROVIDER_LEDGER_WAL].path == (
        history / "market_history.sqlite3.provider-requests.sqlite3-wal"
    )
    assert by_role[ProtectedRootRole.PROVIDER_LEDGER_SHM].path == (
        history / "market_history.sqlite3.provider-requests.sqlite3-shm"
    )
    assert by_role[ProtectedRootRole.CALENDAR_DATABASE_WAL].path == (
        history / "market_history.sqlite3-wal"
    )
    assert by_role[ProtectedRootRole.CALENDAR_DATABASE_SHM].path == (
        history / "market_history.sqlite3-shm"
    )
    assert by_role[ProtectedRootRole.CHECKPOINTS].path == (
        repository_root / "data" / "cache" / "checkpoints"
    )
    assert by_role[ProtectedRootRole.REPORTS_LOGS].path == repository_root / "logs"
    assert by_role[ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS].path == (
        tmp_path / "historical-sandbox-runs"
    )
    assert ProtectedRootRole.FAKE_YAHOO_INCIDENTS in by_role
    assert ProtectedRootRole.ISOLATED_PYTHON_WRITE_PROBE in by_role
    assert ProtectedRootRole.PRIOR_INVENTORIES in by_role
    assert ProtectedRootRole.PRIOR_SANDBOX_QUALIFICATIONS in by_role
    assert ProtectedRootRole.REPLACEMENT_SANDBOX_QUALIFICATION in by_role
    assert ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION in by_role
    assert ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE in by_role
    assert ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION in by_role


@pytest.mark.parametrize(
    ("contract_version", "identity_version", "forward_only_roles"),
    (
        (
            "windows-sandbox-protected-roots-v1",
            "v1",
            frozenset(
                {
                    ProtectedRootRole.QUALIFIED_REPLACEMENT_SANDBOX_QUALIFICATION,
                    ProtectedRootRole.FAILED_ISO04_TICKET06_ACCEPTANCE,
                    ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION,
                }
            ),
        ),
        (
            "windows-sandbox-protected-roots-v2",
            "v2",
            frozenset(
                {ProtectedRootRole.REVIEW_INVALIDATED_SANDBOX_QUALIFICATION}
            ),
        ),
    ),
)
def test_iso_01_historical_protected_manifests_remain_readable(
    tmp_path: Path,
    contract_version: str,
    identity_version: str,
    forward_only_roles: frozenset[ProtectedRootRole],
) -> None:
    forward = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    entries = tuple(
        entry.model_copy(update={"concrete_object_required": True})
        for entry in forward.entries
        if entry.role not in forward_only_roles
    )
    payload = {
        "contract_version": contract_version,
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    material = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    historical = ProtectedRootManifest(
        **payload,
        manifest_identity=(
            f"windows-sandbox-protected-roots:{identity_version}:sha256:"
            + sha256(material).hexdigest()
        ),
    )

    assert ProtectedRootManifest.model_validate_json(
        historical.model_dump_json()
    ) == historical
    assert all(entry.concrete_object_required for entry in historical.entries)


def test_iso_01_missing_required_protected_root_fails_closed(tmp_path: Path) -> None:
    bindings = _protected_bindings(tmp_path / "protected")
    missing = tmp_path / "missing-provider-ledger.sqlite3"
    changed = tuple(
        replace(binding, path=missing)
        if binding.role is ProtectedRootRole.PROVIDER_LEDGER
        else binding
        for binding in bindings
    )

    with pytest.raises(SandboxContractError) as captured:
        build_protected_root_manifest(changed)

    assert captured.value.reason is SandboxFailureReason.PROTECTED_ROOT_MISSING
    assert str(missing) not in str(captured.value)


def test_iso_01_reparse_protected_target_fails_closed(tmp_path: Path) -> None:
    bindings = _protected_bindings(tmp_path / "protected")
    target = bindings[0].path

    class ReparseInspector:
        def inspect(
            self,
            path: Path,
            *,
            expected_kind: ProtectedObjectKind,
        ) -> ProtectedPathMetadata:
            metadata = ProtectedPathMetadata.for_concrete(path, expected_kind=expected_kind)
            return replace(metadata, is_reparse_point=path == target)

    with pytest.raises(SandboxContractError) as captured:
        build_protected_root_manifest(bindings, path_inspector=ReparseInspector())

    assert captured.value.reason is SandboxFailureReason.PROTECTED_ROOT_REPARSE_POINT
    assert str(target) not in str(captured.value)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _reviewed_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "reviewed-repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=reviewed")
    (repository / "package").mkdir()
    (repository / "package" / "module.py").write_text(
        "VALUE = 'reviewed'\n",
        encoding="utf-8",
    )
    (repository / ".env.example").write_text(
        "OPENAI_API_KEY=your_api_key\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "package/module.py", ".env.example")
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "reviewed source",
    )
    return repository


def test_iso_01_source_archive_contains_reviewed_tracked_files_only(
    tmp_path: Path,
) -> None:
    repository = _reviewed_repository(tmp_path)
    (repository / "untracked.txt").write_text("must stay on host\n", encoding="utf-8")

    staged = stage_reviewed_source(repository, tmp_path / "staged-source")

    with tarfile.open(staged.archive_path, mode="r:") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
    assert members == {".env.example", "package/module.py"}
    assert staged.identity.commit_id == _git(repository, "rev-parse", "HEAD")
    assert staged.identity.tree_hash == _git(repository, "rev-parse", "HEAD^{tree}")
    assert staged.identity.tracked_file_count == 2
    assert staged.identity.run_live_environment == ()


def test_iso_01_source_archive_and_tree_identity_are_deterministic(
    tmp_path: Path,
) -> None:
    repository = _reviewed_repository(tmp_path)

    first = stage_reviewed_source(repository, tmp_path / "staged-first")
    second = stage_reviewed_source(repository, tmp_path / "staged-second")

    assert first.identity == second.identity
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.commit_object_path.read_bytes() == second.commit_object_path.read_bytes()


@pytest.mark.parametrize("name", (".env", ".env.enterprise"))
def test_iso_01_tracked_environment_file_is_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    repository = _reviewed_repository(tmp_path)
    (repository / name).write_text("TUSHARE_TOKEN=must-never-enter\n", encoding="utf-8")
    _git(repository, "add", "--", name)
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        f"forbidden {name}",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.FORBIDDEN_INPUT
    assert "must-never-enter" not in str(captured.value)


def test_iso_01_ignored_untracked_environment_file_is_rejected(tmp_path: Path) -> None:
    repository = _reviewed_repository(tmp_path)
    (repository / ".git" / "info" / "exclude").write_text(".env\n", encoding="utf-8")
    (repository / ".env").write_text(
        "TUSHARE_TOKEN=ignored-untracked-canary\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.FORBIDDEN_INPUT
    assert "ignored-untracked-canary" not in str(captured.value)


def test_iso_01_credential_canary_is_rejected_without_value_disclosure(
    tmp_path: Path,
) -> None:
    repository = _reviewed_repository(tmp_path)
    canary = "canary-super-secret-value-123456789"
    (repository / "settings.ini").write_text(
        f"TUSHARE_TOKEN={canary}\n",
        encoding="utf-8",
    )
    _git(repository, "add", "--", "settings.ini")
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "credential canary",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert canary not in str(captured.value)


def test_iso_01_credential_canary_in_reviewed_commit_object_is_rejected(
    tmp_path: Path,
) -> None:
    repository = _reviewed_repository(tmp_path)
    canary = "commit-object-secret-canary-value"
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        f"reviewed metadata\n\nTUSHARE_TOKEN={canary}",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert canary not in str(captured.value)


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("settings.json", '{"TUSHARE_TOKEN":"canary-json-secret-value-123456"}\n'),
        ("settings.ini", "TUSHARE_TOKEN=production-fake-token-value\n"),
        (
            "nested-settings.json",
            '{"safe":1,"TUSHARE_TOKEN":"production-secret-value"}\n',
        ),
        (
            "tests/settings.py",
            'TUSHARE_TOKEN = "real-production-secret"\n',
        ),
    ),
    ids=(
        "quoted-json-key",
        "placeholder-substring",
        "later-minified-json-key",
        "generic-test-secret",
    ),
)
def test_iso_01_credential_scanning_rejects_json_and_placeholder_substring_bypasses(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    repository = _reviewed_repository(tmp_path)
    settings = repository / relative_path
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(content, encoding="utf-8")
    _git(repository, "add", "--", relative_path)
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "credential bypass canary",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert "canary-json-secret" not in str(captured.value)
    assert "production-fake-token" not in str(captured.value)
    assert "production-secret-value" not in str(captured.value)
    assert "real-production-secret" not in str(captured.value)


@pytest.mark.parametrize(
    "content",
    (
        "PIP_INDEX_URL=https://sandbox-user:secret-canary@packages.example.invalid/simple\n",
        ("A" * 2_000_001) + "\nTUSHARE_TOKEN=large-secret-canary-value\n",
    ),
    ids=("credential-url", "large-text"),
)
def test_iso_01_credential_scanning_covers_urls_and_large_tracked_text(
    tmp_path: Path,
    content: str,
) -> None:
    repository = _reviewed_repository(tmp_path)
    settings = repository / "reviewed-settings.txt"
    settings.write_text(content, encoding="utf-8")
    _git(repository, "add", "--", settings.name)
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "forbidden credential input",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert "secret-canary" not in str(captured.value)


def test_iso_01_binary_and_large_untracked_credential_canaries_fail_closed(
    tmp_path: Path,
) -> None:
    binary_root = tmp_path / "binary"
    binary_root.mkdir()
    binary_repository = _reviewed_repository(binary_root)
    canary = b"AKIAABCDEFGHIJKLMNOP"
    binary = binary_repository / "reviewed-binary.bin"
    binary.write_bytes(b"\0binary-prefix\0" + canary)
    _git(binary_repository, "add", "--", binary.name)
    _git(
        binary_repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "forbidden binary credential",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(binary_repository, tmp_path / "staged-binary")
    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert canary.decode("ascii") not in str(captured.value)

    untracked_root = tmp_path / "untracked"
    untracked_root.mkdir()
    untracked_repository = _reviewed_repository(untracked_root)
    untracked = untracked_repository / "local-settings.txt"
    untracked.write_text(
        ("A" * 2_000_001) + "\nTUSHARE_TOKEN=untracked-secret-canary\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(untracked_repository, tmp_path / "staged-untracked")
    assert captured.value.reason is SandboxFailureReason.CREDENTIAL_INPUT
    assert "untracked-secret-canary" not in str(captured.value)


@pytest.mark.parametrize("name", ("market_history.sqlite3", "state.sqlite"))
def test_iso_01_production_runtime_database_input_is_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    repository = _reviewed_repository(tmp_path)
    database = repository / name
    database.write_bytes(b"SQLite format 3\0production-state-canary")
    _git(repository, "add", "--", database.name)
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "forbidden production database",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.FORBIDDEN_INPUT


def test_iso_01_dirty_reviewed_tree_fails_acceptance_staging(tmp_path: Path) -> None:
    repository = _reviewed_repository(tmp_path)
    (repository / "package" / "module.py").write_text(
        "VALUE = 'unreviewed'\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxContractError) as captured:
        stage_reviewed_source(repository, tmp_path / "staged-source")

    assert captured.value.reason is SandboxFailureReason.SOURCE_NOT_CLEAN


def _mapping_bindings(root: Path) -> tuple[SandboxMappingBinding, ...]:
    bindings: list[SandboxMappingBinding] = []
    for role in SandboxMappingRole:
        host_path = root / role.value
        host_path.mkdir(parents=True)
        bindings.append(
            SandboxMappingBinding(
                role=role,
                host_path=host_path,
                guest_path={
                    SandboxMappingRole.SOURCE: r"C:\QCF17\Input\Source",
                    SandboxMappingRole.RUNNER: r"C:\QCF17\Input\Runner",
                    SandboxMappingRole.CONTROL: r"C:\QCF17\Input\Control",
                    SandboxMappingRole.RUNTIME_BUNDLE: r"C:\QCF17\Input\Runtime",
                    SandboxMappingRole.RESULTS_EXPORT: r"C:\QCF17\Export",
                }[role],
                read_only=role is not SandboxMappingRole.RESULTS_EXPORT,
                content_identity=f"mapped-input:v1:sha256:{role.value.encode().hex():0<64}"[:96],
                fresh_empty=role is SandboxMappingRole.RESULTS_EXPORT,
            )
        )
    return tuple(bindings)


def test_iso_01_guest_layout_and_environment_are_entirely_guest_local() -> None:
    layout = build_guest_layout()
    policy = build_environment_policy(
        run_live_environment=(
            "RUN_LIVE_TUSHARE_TESTS",
            "RUN_LIVE_DEEPSEEK_TESTS",
            "RUN_LIVE_PROVIDER_TESTS",
        ),
        layout=layout,
    )
    variables = dict(policy.variables)

    assert layout.contract_version == "windows-sandbox-guest-layout-v1"
    assert all(value.casefold().startswith("c:\\qcf17\\") for value in layout.writable_paths())
    assert all("c:\\users" not in value.casefold() for value in layout.writable_paths())
    assert variables["HOME"] == layout.home
    assert variables["USERPROFILE"] == layout.userprofile
    assert variables["TEMP"] == layout.temp
    assert variables["TMP"] == layout.tmp
    assert variables["APPDATA"] == layout.appdata
    assert variables["LOCALAPPDATA"] == layout.localappdata
    assert variables["TRADINGAGENTS_RESULTS_DIR"] == layout.reports
    assert variables["TRADINGAGENTS_CACHE_DIR"] == layout.tradingagents_cache
    assert variables["TRADINGAGENTS_MARKET_HISTORY_PAYLOAD_ROOT"] == layout.payloads
    assert variables["PYTHONPYCACHEPREFIX"] == layout.python_cache
    assert PureWindowsPath(
        variables["PYTEST_ADDOPTS"].partition("=")[2]
    ) == PureWindowsPath(layout.pytest_temp)
    assert variables["PYTHON_DOTENV_DISABLED"] == "1"
    assert {name: variables[name] for name in variables if name.startswith("RUN_LIVE_")} == {
        "RUN_LIVE_DEEPSEEK_TESTS": "0",
        "RUN_LIVE_PROVIDER_TESTS": "0",
        "RUN_LIVE_TUSHARE_TESTS": "0",
    }


def test_iso_01_pytest_basetemp_survives_pytest_addopts_parsing() -> None:
    layout = build_guest_layout()
    policy = build_environment_policy(
        run_live_environment=(),
        layout=layout,
    )

    parsed = shlex.split(dict(policy.variables)["PYTEST_ADDOPTS"])

    assert parsed == ["--basetemp=C:/QCF17/Runtime/PytestTemp"]
    assert PureWindowsPath(parsed[0].partition("=")[2]) == PureWindowsPath(
        layout.pytest_temp
    )


def test_iso_01_mapping_manifest_has_four_read_only_inputs_and_one_writable_export(
    tmp_path: Path,
) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    bindings = _mapping_bindings(tmp_path / "mappings")

    manifest = build_mapping_manifest(
        bindings,
        protected_manifest=protected,
        forbidden_host_roots=(tmp_path / "host-profile", tmp_path / "host-venv"),
    )

    assert tuple(mapping.role for mapping in manifest.mappings) == tuple(SandboxMappingRole)
    assert all(mapping.read_only for mapping in manifest.mappings[:-1])
    assert manifest.mappings[-1].role is SandboxMappingRole.RESULTS_EXPORT
    assert manifest.mappings[-1].read_only is False
    assert sum(not mapping.read_only for mapping in manifest.mappings) == 1


def test_iso_01_wsb_configuration_disables_network_and_host_integrations(
    tmp_path: Path,
) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    manifest = build_mapping_manifest(
        _mapping_bindings(tmp_path / "mappings"),
        protected_manifest=protected,
        forbidden_host_roots=(),
    )

    configuration = render_wsb_configuration(
        manifest,
        protected_client_supported=True,
    )
    xml = ElementTree.fromstring(configuration.xml)

    assert xml.findtext("Networking") == "Disable"
    assert xml.findtext("vGPU") == "Disable"
    assert xml.find("VGpu") is None
    assert xml.findtext("AudioInput") == "Disable"
    assert xml.findtext("VideoInput") == "Disable"
    assert xml.findtext("PrinterRedirection") == "Disable"
    assert xml.findtext("ClipboardRedirection") == "Disable"
    assert xml.findtext("ProtectedClient") == "Enable"
    mapped = xml.findall("./MappedFolders/MappedFolder")
    assert len(mapped) == 5
    assert [folder.findtext("ReadOnly") for folder in mapped] == [
        "true",
        "true",
        "true",
        "true",
        "false",
    ]
    assert "bootstrap.ps1" in (xml.findtext("./LogonCommand/Command") or "")
    assert configuration.config_identity.startswith("windows-sandbox-config:v1:sha256:")

    unsupported = render_wsb_configuration(
        manifest,
        protected_client_supported=False,
    )
    unsupported_xml = ElementTree.fromstring(unsupported.xml)
    assert unsupported_xml.find("ProtectedClient") is None
    validate_wsb_configuration(
        unsupported.xml,
        mapping_manifest=manifest,
        protected_client_supported=False,
    )


@pytest.mark.parametrize(
    ("role", "read_only", "reason"),
    (
        (
            SandboxMappingRole.SOURCE,
            False,
            SandboxFailureReason.WRITABLE_INPUT_MAPPING,
        ),
        (
            SandboxMappingRole.RUNNER,
            False,
            SandboxFailureReason.WRITABLE_INPUT_MAPPING,
        ),
        (
            SandboxMappingRole.CONTROL,
            False,
            SandboxFailureReason.WRITABLE_INPUT_MAPPING,
        ),
        (
            SandboxMappingRole.RUNTIME_BUNDLE,
            False,
            SandboxFailureReason.WRITABLE_INPUT_MAPPING,
        ),
    ),
)
def test_iso_01_every_input_mapping_is_read_only(
    tmp_path: Path,
    role: SandboxMappingRole,
    read_only: bool,
    reason: SandboxFailureReason,
) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    bindings = tuple(
        replace(binding, read_only=read_only) if binding.role is role else binding
        for binding in _mapping_bindings(tmp_path / "mappings")
    )

    with pytest.raises(SandboxContractError) as captured:
        build_mapping_manifest(
            bindings,
            protected_manifest=protected,
            forbidden_host_roots=(),
        )

    assert captured.value.reason is reason


def test_iso_01_additional_writable_host_mapping_fails(tmp_path: Path) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    bindings = _mapping_bindings(tmp_path / "mappings")
    extra = replace(
        bindings[0],
        role=SandboxMappingRole.SOURCE,
        host_path=tmp_path / "extra-writable",
        guest_path=r"C:\QCF17\Extra",
        read_only=False,
    )
    extra.host_path.mkdir()

    with pytest.raises(SandboxContractError) as captured:
        build_mapping_manifest(
            (*bindings, extra),
            protected_manifest=protected,
            forbidden_host_roots=(),
        )

    assert captured.value.reason in {
        SandboxFailureReason.MAPPING_CONTRACT_INVALID,
        SandboxFailureReason.ADDITIONAL_WRITABLE_MAPPING,
    }


def test_iso_01_nonempty_or_nonfresh_results_export_fails(tmp_path: Path) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    bindings = list(_mapping_bindings(tmp_path / "mappings"))
    results = bindings[-1]
    (results.host_path / "old-result.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SandboxContractError) as captured:
        build_mapping_manifest(
            tuple(bindings),
            protected_manifest=protected,
            forbidden_host_roots=(),
        )

    assert captured.value.reason is SandboxFailureReason.RESULTS_EXPORT_NOT_FRESH


def test_iso_01_protected_or_forbidden_host_mapping_fails(tmp_path: Path) -> None:
    protected_bindings = _protected_bindings(tmp_path / "protected")
    protected = build_protected_root_manifest(protected_bindings)
    mappings = list(_mapping_bindings(tmp_path / "mappings"))
    mappings[0] = replace(mappings[0], host_path=protected_bindings[0].path)

    with pytest.raises(SandboxContractError) as captured:
        build_mapping_manifest(
            tuple(mappings),
            protected_manifest=protected,
            forbidden_host_roots=(),
        )
    assert captured.value.reason is SandboxFailureReason.PROTECTED_HOST_MAPPING

    for name in ("host-profile", "host-venv"):
        forbidden_root = tmp_path / name
        forbidden_root.mkdir()
        mappings = list(_mapping_bindings(tmp_path / f"{name}-mappings"))
        mappings[0] = replace(mappings[0], host_path=forbidden_root)
        with pytest.raises(SandboxContractError) as captured:
            build_mapping_manifest(
                tuple(mappings),
                protected_manifest=protected,
                forbidden_host_roots=(forbidden_root,),
            )
        assert captured.value.reason is SandboxFailureReason.FORBIDDEN_HOST_MAPPING


def test_iso_01_overlapping_host_or_guest_mappings_fail(tmp_path: Path) -> None:
    protected = build_protected_root_manifest(_protected_bindings(tmp_path / "protected"))
    mappings = list(_mapping_bindings(tmp_path / "mappings"))
    mappings[1] = replace(
        mappings[1],
        host_path=mappings[0].host_path / "nested",
    )
    mappings[1].host_path.mkdir()

    with pytest.raises(SandboxContractError) as captured:
        build_mapping_manifest(
            tuple(mappings),
            protected_manifest=protected,
            forbidden_host_roots=(),
        )

    assert captured.value.reason is SandboxFailureReason.OVERLAPPING_MAPPING


def _material_identity(namespace: str, marker: str) -> str:
    return f"{namespace}:sha256:{marker * 64}"


def _identity_for_test(namespace: str, value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{namespace}:sha256:{sha256(encoded).hexdigest()}"


def _runner_material() -> RunnerIdentityMaterial:
    return RunnerIdentityMaterial(
        protected_root_manifest_identity=_material_identity("protected", "1"),
        source_input_identity=_material_identity("source", "2"),
        wsb_config_identity=_material_identity("config", "3"),
        mapping_manifest_identity=_material_identity("mappings", "4"),
        guest_layout_identity=_material_identity("layout", "5"),
        environment_policy_identity=_material_identity("environment", "6"),
        result_schema_identity=_material_identity("result-schema", "7"),
        runtime_bundle_contract_identity=_material_identity("runtime", "8"),
        runner_bootstrap_identity=_material_identity("bootstrap", "9"),
        control_inventory_identity=_material_identity("control", "a"),
    )


def _result_template(mode: SandboxExecutionMode = SandboxExecutionMode.BOUNDARY_QUALIFICATION):
    material = _runner_material()
    runner_identity = build_runner_identity(material)
    return build_guest_result_template(
        execution_mode=mode,
        runner_identity=runner_identity,
        wsb_config_identity=material.wsb_config_identity,
        source_commit_id="b" * 40,
        source_tree_hash="c" * 40,
        source_archive_sha256="d" * 64,
        source_manifest_identity=_material_identity("source-manifest", "e"),
        protected_root_manifest_identity=material.protected_root_manifest_identity,
        runtime_bundle_contract_identity=material.runtime_bundle_contract_identity,
        mapping_manifest_identity=material.mapping_manifest_identity,
        guest_workspace_identity=_material_identity("workspace", "f"),
    )


def _write_result_export(root: Path, result: SandboxGuestResult) -> None:
    root.mkdir()
    result_bytes = result.canonical_json_bytes() + b"\n"
    (root / "result.json").write_bytes(result_bytes)
    marker = build_completion_marker(result=result, result_bytes=result_bytes)
    (root / "completion.json").write_bytes(marker.canonical_json_bytes() + b"\n")


def test_iso_01_runtime_bundle_placeholder_is_read_only_offline_contract() -> None:
    contract = build_runtime_bundle_placeholder()

    assert contract.contract_version == "windows-sandbox-runtime-bundle-input-v1"
    assert contract.state == "placeholder_for_iso_02"
    assert contract.mapped_read_only is True
    assert contract.offline_required is True
    assert contract.network_install_permitted is False
    assert contract.runtime_bundle_contract_identity.startswith(
        "windows-sandbox-runtime-bundle-input:v1:sha256:"
    )


@pytest.mark.parametrize(
    "updates",
    (
        {"bootstrap_relative_path": r"..\escape.ps1"},
        {"python_install_target": r"C:\Users\host\Python"},
    ),
)
def test_iso_01_runtime_bundle_fill_contract_rejects_escape_paths(
    updates: dict[str, str],
) -> None:
    layout = build_guest_layout()
    payload = {
        "state": "sealed",
        "python_install_target": layout.python_install,
        "virtual_environment_target": layout.virtual_environment,
        "bundle_manifest_identity": f"runtime-bundle-manifest:v1:sha256:{'1' * 64}",
        "bootstrap_relative_path": "install-runtime.ps1",
        "git_relative_path": r"Git\cmd\git.exe",
        "runtime_bundle_contract_identity": (
            f"windows-sandbox-runtime-bundle-input:v1:sha256:{'0' * 64}"
        ),
    }
    payload.update(updates)

    with pytest.raises(ValueError, match="runtime bundle (target|relative path)"):
        RuntimeBundleInputContract(**payload)


@pytest.mark.parametrize(
    "mode",
    (
        SandboxExecutionMode.BOUNDARY_QUALIFICATION,
        SandboxExecutionMode.TICKET06_ACCEPTANCE,
    ),
)
def test_iso_01_result_schema_is_closed_bounded_and_mode_explicit(
    mode: SandboxExecutionMode,
) -> None:
    result = _result_template(mode)
    payload = json.loads(result.canonical_json_bytes())

    assert payload["execution_mode"] == mode.value
    assert payload["completion_marker_contract_identity"].startswith(
        "windows-sandbox-completion-marker:v1:sha256:"
    )
    assert {
        "probes",
        "qcf",
        "pe",
        "ac",
        "pytest",
        "ruff",
        "diff",
    }.issubset(payload)
    assert all(
        payload[name]["status"] == "not_run"
        for name in ("probes", "qcf", "pe", "ac", "pytest", "ruff", "diff")
    )
    payload["undeclared"] = "not allowed"
    with pytest.raises(ValueError):
        SandboxGuestResult.model_validate(payload)


def test_iso_01_completion_marker_is_last_and_guest_pass_is_not_host_authority(
    tmp_path: Path,
) -> None:
    result = _result_template()
    export = tmp_path / "export"
    _write_result_export(export, result)

    validated = validate_result_export(export, expected=result)

    assert validated.result == result
    assert validated.completion.write_order == "last"
    assert validated.host_qcf17_authority is False
    assert validated.export_identity.startswith("windows-sandbox-result-export:v1:sha256:")


def test_iso_01_populated_bounded_result_preserves_static_identity_bindings(
    tmp_path: Path,
) -> None:
    template = _result_template()
    populated = finalize_guest_result(
        template,
        guest_claim="pass",
        probes=BoundedResultSection(
            status=ResultSectionStatus.PASS,
            total=1,
            passed=1,
            record_identities=(_material_identity("probe", "1"),),
            summary_identity=_material_identity("probe-summary", "2"),
        ),
    )
    export = tmp_path / "export"
    _write_result_export(export, populated)

    validated = validate_result_export(export, expected=template)

    assert validated.result == populated
    assert validated.result.probes.status is ResultSectionStatus.PASS
    assert validated.host_qcf17_authority is False


def test_iso_01_result_export_rejects_raw_guest_output_in_identity_fields(
    tmp_path: Path,
) -> None:
    expected = _result_template()
    valid = finalize_guest_result(
        expected,
        guest_claim="pass",
        probes=BoundedResultSection(
            status=ResultSectionStatus.PASS,
            total=1,
            passed=1,
            record_identities=(_material_identity("probe", "1"),),
        ),
    )
    payload = json.loads(valid.canonical_json_bytes())
    payload["probes"]["record_identities"] = ["raw-guest-output-canary"]
    payload["probes"]["summary_identity"] = "raw-guest-output-canary"
    identity_payload = {key: value for key, value in payload.items() if key != "result_identity"}
    payload["result_identity"] = _identity_for_test(
        "windows-sandbox-result:v1",
        identity_payload,
    )
    result_bytes = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    marker_payload = {
        "contract_version": "windows-sandbox-completion-marker-v1",
        "completion_marker_contract_identity": payload["completion_marker_contract_identity"],
        "result_identity": payload["result_identity"],
        "result_sha256": sha256(result_bytes).hexdigest(),
        "write_order": "last",
    }
    marker_payload["marker_identity"] = _identity_for_test(
        "windows-sandbox-completion:v1",
        marker_payload,
    )
    export = tmp_path / "export"
    export.mkdir()
    (export / "result.json").write_bytes(result_bytes)
    (export / "completion.json").write_text(
        json.dumps(marker_payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SandboxContractError) as captured:
        validate_result_export(export, expected=expected)

    assert captured.value.reason is SandboxFailureReason.COMPLETION_MARKER_INVALID
    assert "raw-guest-output-canary" not in str(captured.value)


@pytest.mark.parametrize(
    "section",
    (
        {"status": ResultSectionStatus.PASS, "total": 1, "passed": 0, "failed": 1},
        {"status": ResultSectionStatus.FAIL, "total": 1, "passed": 1, "failed": 0},
    ),
    ids=("pass-with-failure", "fail-without-failure"),
)
def test_iso_01_result_section_status_and_counts_cannot_contradict(
    section: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="status and counts are inconsistent"):
        BoundedResultSection(**section)


@pytest.mark.parametrize(
    ("name", "content"),
    (
        ("market_history.sqlite3", b"production database"),
        ("unsafe.log", b"unbounded log"),
        ("credentials.json", b'{"TUSHARE_TOKEN":"canary-secret-value-123456"}'),
    ),
)
def test_iso_01_result_export_rejects_runtime_state_secrets_and_unsafe_logs(
    tmp_path: Path,
    name: str,
    content: bytes,
) -> None:
    result = _result_template()
    export = tmp_path / "export"
    _write_result_export(export, result)
    (export / name).write_bytes(content)

    with pytest.raises(SandboxContractError) as captured:
        validate_result_export(export, expected=result)

    assert captured.value.reason is SandboxFailureReason.UNSAFE_RESULT_EXPORT
    assert "canary-secret-value" not in str(captured.value)


def test_iso_01_completion_marker_digest_or_write_order_mismatch_fails(
    tmp_path: Path,
) -> None:
    result = _result_template()
    export = tmp_path / "export"
    _write_result_export(export, result)
    marker_path = export / "completion.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["result_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SandboxContractError) as captured:
        validate_result_export(export, expected=result)

    assert captured.value.reason is SandboxFailureReason.COMPLETION_MARKER_INVALID


def test_iso_01_runner_identity_changes_for_every_material_policy_change() -> None:
    material = _runner_material()
    baseline = build_runner_identity(material)

    for field_name in (
        "protected_root_manifest_identity",
        "source_input_identity",
        "wsb_config_identity",
        "mapping_manifest_identity",
        "guest_layout_identity",
        "environment_policy_identity",
        "result_schema_identity",
        "runtime_bundle_contract_identity",
        "runner_bootstrap_identity",
        "control_inventory_identity",
    ):
        changed = material.model_copy(update={field_name: _material_identity(field_name, "0")})
        assert build_runner_identity(changed) != baseline


def test_iso_01_runner_identity_is_stable_for_equivalent_material() -> None:
    material = _runner_material()

    assert build_runner_identity(material) == build_runner_identity(
        RunnerIdentityMaterial.model_validate(
            dict(reversed(list(material.model_dump(mode="json").items())))
        )
    )


def _runner_ready_repository(tmp_path: Path) -> Path:
    repository = _reviewed_repository(tmp_path)
    assets = Path(__file__).resolve().parents[1] / "scripts" / "windows_sandbox"
    target = repository / "scripts" / "windows_sandbox"
    target.mkdir(parents=True)
    for name in ("bootstrap.ps1", "verify_source.py"):
        (target / name).write_bytes((assets / name).read_bytes())
    (repository / "tests").mkdir()
    (repository / "tests" / "test_live_contract.py").write_text(
        'OPT_IN = "RUN_LIVE_CUSTOM_TESTS"\nAWS_DOCUMENTATION_CANARY = "AKIAABCDEFGHIJKLMNOP"\n',
        encoding="utf-8",
    )
    _git(
        repository,
        "add",
        "--",
        "scripts/windows_sandbox/bootstrap.ps1",
        "scripts/windows_sandbox/verify_source.py",
        "tests/test_live_contract.py",
    )
    _git(
        repository,
        "-c",
        "user.name=ISO Test",
        "-c",
        "user.email=iso@example.invalid",
        "commit",
        "-m",
        "reviewed runner assets",
    )
    return repository


class _FakeSandboxPlatform:
    def __init__(self, report: SandboxPlatformReport) -> None:
        self.report = report
        self.probe_calls = 0
        self.launch_calls = 0

    def probe(self) -> SandboxPlatformReport:
        self.probe_calls += 1
        return self.report

    def launch(self, config_path: Path, *, timeout_seconds: int):
        self.launch_calls += 1
        raise AssertionError("ISO-01 must not launch Windows Sandbox")


def test_iso_01_real_platform_probe_has_an_injected_no_launch_seam(tmp_path: Path) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed-sandbox-executable-fixture")
    platform = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=22621,
        feature_state_override=SandboxFeatureState.ENABLED,
    )

    report = platform.probe()

    assert report.state is SandboxPlatformState.AVAILABLE
    assert report.protected_client_supported is True
    assert report.executable_identity is not None
    assert "LogonCommand" in report.required_settings_supported


@pytest.mark.parametrize(
    ("feature_state", "expected_state"),
    (
        (SandboxFeatureState.DISABLED, SandboxPlatformState.NOT_QUALIFIED),
        (SandboxFeatureState.UNKNOWN, SandboxPlatformState.NEEDS_INFO),
    ),
)
def test_iso_01_platform_feature_state_is_typed_without_launch(
    tmp_path: Path,
    feature_state: SandboxFeatureState,
    expected_state: SandboxPlatformState,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed-sandbox-executable-fixture")

    report = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=22621,
        feature_state_override=feature_state,
    ).probe()

    assert report.state is expected_state


@pytest.mark.parametrize("failure", ("missing", "unreadable"))
def test_iso_01_platform_executable_filesystem_failures_are_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    if failure == "unreadable":
        executable.write_bytes(b"reviewed-sandbox-executable-fixture")
        original_read_bytes = Path.read_bytes

        def fail_target_read(path: Path) -> bytes:
            if path == executable:
                raise OSError("path-bearing failure must be bounded")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", fail_target_read)

    report = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=22621,
        feature_state_override=SandboxFeatureState.ENABLED,
    ).probe()

    assert report.state is SandboxPlatformState.NEEDS_INFO
    assert report.diagnostic_code == "sandbox-executable-unreadable"


def test_iso_01_required_wsb_setting_support_is_not_inferred_on_old_build(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "WindowsSandbox.exe"
    executable.write_bytes(b"reviewed-sandbox-executable-fixture")

    report = WindowsSandboxPlatform(
        host_is_windows=True,
        executable_override=executable,
        windows_build=19045,
        feature_state_override=SandboxFeatureState.ENABLED,
    ).probe()

    assert report.state is SandboxPlatformState.NOT_QUALIFIED
    assert report.diagnostic_code == "required-wsb-settings-unsupported"
    assert report.required_settings_supported == ()


def _construction_request(tmp_path: Path) -> RunnerConstructionRequest:
    repository = _runner_ready_repository(tmp_path)
    runs_root = tmp_path / "sandbox-runs"
    runs_root.mkdir()
    user_profile = tmp_path / "host-profile"
    user_profile.mkdir()
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()
    host_venv = repository / ".venv"
    host_venv.mkdir()
    return RunnerConstructionRequest(
        repository_root=repository,
        runs_root=runs_root,
        run_id="iso01-test-run-0001",
        execution_mode=SandboxExecutionMode.BOUNDARY_QUALIFICATION,
        protected_root_bindings=_protected_bindings(tmp_path / "protected"),
        user_profile=user_profile,
        host_temp=host_temp,
        host_venv=host_venv,
    )


def test_iso_01_host_service_constructs_without_launching_or_executing_acceptance(
    tmp_path: Path,
) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    service = WindowsSandboxRunnerService(platform=platform)

    contract = service.validate_only(_construction_request(tmp_path))

    assert platform.probe_calls == 1
    assert platform.launch_calls == 0
    assert contract.qualification_outcome is SandboxQualificationOutcome.CONSTRUCTION_VALIDATED
    assert contract.runtime_bundle.state == "placeholder_for_iso_02"
    assert contract.paths.results_export.is_dir()
    assert list(contract.paths.results_export.iterdir()) == []
    assert sum(not item.read_only for item in contract.mapping_manifest.mappings) == 1
    assert contract.config_path.read_text(encoding="utf-8") == contract.wsb_configuration.xml
    assert contract.runner_identity.startswith("iso01-windows-sandbox-runner:v1:sha256:")
    assert contract.no_launch_output_identity.startswith(
        "windows-sandbox-no-launch-validation:v1:sha256:"
    )
    variables = dict(contract.environment_policy.variables)
    assert variables["RUN_LIVE_CUSTOM_TESTS"] == "0"
    control = contract.paths.control_input.joinpath("run-control.json").read_text(encoding="utf-8")
    control_payload = json.loads(control)
    assert str(tmp_path) not in control
    assert "mapping_definitions" not in control_payload
    assert control_payload["wsb_policy"]["networking"] == "Disable"

    bootstrap = (
        contract.paths.runner_input.joinpath("bootstrap.ps1").read_text(encoding="utf-8").casefold()
    )
    assert '"boundary_qualification"' in bootstrap
    assert '"ticket06_acceptance"' in bootstrap
    for forbidden in (
        "ticket07",
        "ticket 07",
        "qualified_v1",
        "pytest",
        "ruff",
        "tradingagents.exe",
    ):
        assert forbidden not in bootstrap


def test_iso_01_guest_reconstruction_matches_reviewed_tree_before_execution(
    tmp_path: Path,
) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    contract = WindowsSandboxRunnerService(platform=platform).validate_only(
        _construction_request(tmp_path)
    )
    destination = tmp_path / "guest" / "Repository"
    verification = tmp_path / "guest" / "workspace-verification.json"

    subprocess.run(
        [
            sys.executable,
            str(contract.paths.runner_input / "verify_source.py"),
            "--source-input",
            str(contract.paths.source_input),
            "--destination",
            str(destination),
            "--verification-output",
            str(verification),
        ],
        check=True,
        capture_output=True,
    )
    payload = json.loads(verification.read_text(encoding="utf-8"))

    assert payload["commit_id"] == contract.source.identity.commit_id
    assert payload["tree_hash"] == contract.source.identity.tree_hash
    assert payload["synthetic_git_worktree"] is True
    assert payload["project_code_imported"] is False
    assert (destination / ".git" / "HEAD").read_text(encoding="ascii") == (
        "ref: refs/heads/reviewed\n"
    )


def test_iso_01_placeholder_runtime_prevents_launch_contract_use(tmp_path: Path) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    service = WindowsSandboxRunnerService(platform=platform)
    contract = service.validate_only(_construction_request(tmp_path))

    with pytest.raises(SandboxContractError) as captured:
        service.launch(contract, timeout_seconds=30)

    assert captured.value.reason is SandboxFailureReason.RUNTIME_BUNDLE_NOT_READY
    assert platform.launch_calls == 0


@pytest.mark.parametrize("target", ("config", "source", "environment"))
def test_iso_01_launch_contract_revalidates_every_material_input_before_use(
    tmp_path: Path,
    target: str,
) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    service = WindowsSandboxRunnerService(platform=platform)
    contract = service.validate_only(_construction_request(tmp_path))
    if target == "config":
        contract.config_path.write_text(
            contract.wsb_configuration.xml.replace(
                "<Networking>Disable</Networking>",
                "<Networking>Enable</Networking>",
            ),
            encoding="utf-8",
            newline="\n",
        )
    elif target == "source":
        with contract.source.archive_path.open("ab") as handle:
            handle.write(b"tampered")
    else:
        contract = replace(
            contract,
            environment_policy=build_environment_policy(
                run_live_environment=(),
                layout=contract.guest_layout,
            ),
        )

    with pytest.raises(SandboxContractError) as captured:
        service.launch(contract, timeout_seconds=30)

    expected = {
        "config": SandboxFailureReason.PLATFORM_CONTRACT_INVALID,
        "source": SandboxFailureReason.SOURCE_IDENTITY_MISMATCH,
        "environment": SandboxFailureReason.ENVIRONMENT_POLICY_INVALID,
    }[target]
    assert captured.value.reason is expected
    assert platform.launch_calls == 0


def test_iso_01_invalid_execution_mode_fails_typed_preflight(tmp_path: Path) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    request = replace(_construction_request(tmp_path), execution_mode="invalid")

    with pytest.raises(SandboxContractError) as captured:
        WindowsSandboxRunnerService(platform=platform).validate_only(request)

    assert captured.value.reason is SandboxFailureReason.MAPPING_CONTRACT_INVALID
    assert platform.launch_calls == 0


@pytest.mark.parametrize(
    ("field_name", "reason"),
    (
        ("repository_root", SandboxFailureReason.SOURCE_REPOSITORY_INVALID),
        ("runs_root", SandboxFailureReason.MAPPING_CONTRACT_INVALID),
        ("user_profile", SandboxFailureReason.MAPPING_CONTRACT_INVALID),
        ("host_temp", SandboxFailureReason.MAPPING_CONTRACT_INVALID),
        ("host_venv", SandboxFailureReason.MAPPING_CONTRACT_INVALID),
    ),
)
def test_iso_01_missing_host_preflight_paths_fail_typed_and_path_redacted(
    tmp_path: Path,
    field_name: str,
    reason: SandboxFailureReason,
) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    missing = tmp_path / f"missing-{field_name}"
    request = replace(_construction_request(tmp_path), **{field_name: missing})

    with pytest.raises(SandboxContractError) as captured:
        WindowsSandboxRunnerService(platform=platform).validate_only(request)

    assert captured.value.reason is reason
    assert str(missing) not in str(captured.value)
    assert platform.launch_calls == 0


@pytest.mark.parametrize(
    "state",
    (SandboxPlatformState.NEEDS_INFO, SandboxPlatformState.NOT_QUALIFIED),
)
def test_iso_01_platform_unavailable_is_typed_without_machine_launch_dependency(
    tmp_path: Path,
    state: SandboxPlatformState,
) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.unavailable(state=state, diagnostic_code="platform-unavailable")
    )
    service = WindowsSandboxRunnerService(platform=platform)

    with pytest.raises(SandboxContractError) as captured:
        service.validate_only(_construction_request(tmp_path))

    assert captured.value.reason is SandboxFailureReason.PLATFORM_UNAVAILABLE
    assert captured.value.qualification_outcome.value == state.value
    assert platform.launch_calls == 0


def test_iso_01_network_enabled_or_mapping_weakened_wsb_fails(tmp_path: Path) -> None:
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )
    contract = WindowsSandboxRunnerService(platform=platform).validate_only(
        _construction_request(tmp_path)
    )
    network_enabled = contract.wsb_configuration.xml.replace(
        "<Networking>Disable</Networking>",
        "<Networking>Enable</Networking>",
    )

    with pytest.raises(SandboxContractError) as captured:
        validate_wsb_configuration(
            network_enabled,
            mapping_manifest=contract.mapping_manifest,
            protected_client_supported=True,
        )
    assert captured.value.reason is SandboxFailureReason.NETWORKING_ENABLED

    writable_source = contract.wsb_configuration.xml.replace(
        "<ReadOnly>true</ReadOnly>",
        "<ReadOnly>false</ReadOnly>",
        1,
    )
    with pytest.raises(SandboxContractError) as captured:
        validate_wsb_configuration(
            writable_source,
            mapping_manifest=contract.mapping_manifest,
            protected_client_supported=True,
        )
    assert captured.value.reason is SandboxFailureReason.WRITABLE_INPUT_MAPPING


def test_iso_01_no_launch_cli_emits_bounded_safe_construction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = _construction_request(tmp_path)
    (tmp_path / "cli-runs").mkdir()
    monkeypatch.setenv("USERPROFILE", str(request.user_profile))
    monkeypatch.setattr(
        run_iso01_windows_sandbox,
        "default_protected_root_bindings",
        lambda **_kwargs: request.protected_root_bindings,
    )
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )

    exit_code = run_iso01_windows_sandbox.main(
        [
            "validate",
            "--repository-root",
            str(request.repository_root),
            "--runs-root",
            str(tmp_path / "cli-runs"),
            "--run-id",
            "iso01-cli-run-0001",
            "--execution-mode",
            "boundary_qualification",
        ],
        platform=platform,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "construction-validated"
    assert payload["sandbox_launched"] is False
    assert payload["ticket06_executed"] is False
    assert payload["ticket07_executed"] is False
    assert payload["provider_model_io"] is False
    assert payload["writable_host_mapping_count"] == 1
    assert payload["networking"] == "Disable"
    assert str(tmp_path) not in json.dumps(payload)
    assert platform.launch_calls == 0


def test_iso_01_no_launch_cli_platform_failure_is_typed_and_path_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _runner_ready_repository(tmp_path)
    runs_root = tmp_path / "cli-runs"
    runs_root.mkdir()
    user_profile = tmp_path / "profile"
    user_profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.unavailable(
            state=SandboxPlatformState.NEEDS_INFO,
            diagnostic_code="sandbox-unavailable",
        )
    )

    exit_code = run_iso01_windows_sandbox.main(
        [
            "validate",
            "--repository-root",
            str(repository),
            "--runs-root",
            str(runs_root),
            "--run-id",
            "iso01-cli-run-0002",
            "--execution-mode",
            "boundary_qualification",
        ],
        platform=platform,
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs-info"
    assert payload["reason"] == "platform_unavailable"
    assert str(tmp_path) not in json.dumps(payload)
    assert platform.launch_calls == 0


@pytest.mark.parametrize(
    ("missing_argument", "expected_reason"),
    (
        ("repository", "source_repository_invalid"),
        ("profile", "mapping_contract_invalid"),
    ),
)
def test_iso_01_no_launch_cli_filesystem_preflight_is_typed_and_path_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing_argument: str,
    expected_reason: str,
) -> None:
    repository = _runner_ready_repository(tmp_path)
    runs_root = tmp_path / "cli-runs"
    runs_root.mkdir()
    user_profile = tmp_path / "profile"
    user_profile.mkdir()
    if missing_argument == "repository":
        repository = tmp_path / "missing-repository"
    else:
        user_profile = tmp_path / "missing-profile"
    monkeypatch.setenv("USERPROFILE", str(user_profile))
    platform = _FakeSandboxPlatform(
        SandboxPlatformReport.available(protected_client_supported=True)
    )

    exit_code = run_iso01_windows_sandbox.main(
        [
            "validate",
            "--repository-root",
            str(repository),
            "--runs-root",
            str(runs_root),
            "--run-id",
            "iso01-cli-fs-failure-0001",
            "--execution-mode",
            "boundary_qualification",
        ],
        platform=platform,
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not-qualified"
    assert payload["reason"] == expected_reason
    assert str(tmp_path) not in json.dumps(payload)
    assert platform.launch_calls == 0

"""Build one reviewed ISO-02 bundle from already-local offline artifacts."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from tradingagents.offline_runtime_bundle import (
    OfflineBundleSourceBinding,
    OfflineRuntimeBundleBuildRequest,
    build_offline_runtime_bundle,
)
from tradingagents.windows_sandbox_runner import stage_reviewed_head_objects

_BOOTSTRAP_TOOL_ROOTS = ("pip==25.2", "wheel==0.46.3")
_PORTABLE_GIT_EXTRACTION_MISMATCH = "reviewed_portable_git_extraction_mismatch"


def _git_blob(repository: Path, object_id: str) -> bytes:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=NUL",
            "cat-file",
            "blob",
            object_id,
        ],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        shell=False,
    )
    return completed.stdout


def validate_portable_git_extraction(archive_path: Path, extracted_root: Path) -> None:
    """Prove the distributed MinGit files are exactly from the hash-locked archive."""

    try:
        root_metadata = extracted_root.lstat()
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
        with zipfile.ZipFile(archive_path) as archive:
            archived: dict[str, tuple[str, zipfile.ZipInfo]] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                relative = PurePosixPath(info.filename)
                key = relative.as_posix().casefold()
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or key in archived
                ):
                    raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
                archived[key] = (relative.as_posix(), info)

            extracted: dict[str, tuple[str, Path]] = {}
            for path in extracted_root.rglob("*"):
                metadata = path.lstat()
                if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
                    continue
                relative = path.relative_to(extracted_root).as_posix()
                key = relative.casefold()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or key in extracted
                ):
                    raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
                extracted[key] = (relative, path)

            if set(archived) != set(extracted):
                raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
            for key in sorted(archived):
                archived_relative, info = archived[key]
                extracted_relative, path = extracted[key]
                if archived_relative != extracted_relative or info.file_size != path.stat().st_size:
                    raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
                if sha256(archive.read(info)).digest() != sha256(path.read_bytes()).digest():
                    raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH)
    except RuntimeError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(_PORTABLE_GIT_EXTRACTION_MISMATCH) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build ISO-02 from reviewed local inputs; this command never downloads.",
    )
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--python-installer", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--portable-git-root", required=True, type=Path)
    parser.add_argument("--portable-git-archive", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository = arguments.repository_root.expanduser().resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="iso02-reviewed-inputs-") as temporary:
        temporary_root = Path(temporary)
        staged = stage_reviewed_head_objects(repository, temporary_root / "source")
        by_path = {entry.relative_path: entry for entry in staged.manifest.entries}
        required_paths = {
            "pyproject.toml",
            "scripts/windows_sandbox/offline_bundle_bootstrap.ps1",
            "scripts/windows_sandbox/verify_offline_bundle.py",
            "scripts/windows_sandbox/iso02-artifact-lock.json",
            "scripts/windows_sandbox/iso02-requirements-win-cp313.txt",
        }
        if not required_paths.issubset(by_path):
            raise RuntimeError("reviewed_iso02_source_inputs_missing")
        project_metadata = _git_blob(repository, by_path["pyproject.toml"].git_object_id)
        artifact_lock_bytes = _git_blob(
            repository,
            by_path["scripts/windows_sandbox/iso02-artifact-lock.json"].git_object_id,
        )
        artifact_lock = json.loads(artifact_lock_bytes)
        for path, expected in (
            (arguments.python_installer, artifact_lock["python"]),
            (arguments.portable_git_archive, artifact_lock["portable_git"]),
        ):
            content = path.read_bytes()
            expected_name = expected.get("artifact_filename", expected.get("archive_filename"))
            expected_length = expected.get(
                "artifact_byte_length", expected.get("archive_byte_length")
            )
            expected_sha256 = expected.get("artifact_sha256", expected.get("archive_sha256"))
            if (
                path.name != expected_name
                or len(content) != expected_length
                or sha256(content).hexdigest() != expected_sha256
            ):
                raise RuntimeError("reviewed_runtime_artifact_mismatch")
        validate_portable_git_extraction(
            arguments.portable_git_archive,
            arguments.portable_git_root,
        )
        project = tomllib.loads(project_metadata.decode("utf-8"))["project"]
        dependency_roots = tuple(
            sorted(
                (
                    *project["dependencies"],
                    *project["optional-dependencies"]["dev"],
                    *project["optional-dependencies"]["pit"],
                    *_BOOTSTRAP_TOOL_ROOTS,
                )
            )
        )
        support_root = temporary_root / "support"
        support_root.mkdir()
        bootstrap_script = support_root / "install-runtime.ps1"
        verifier_script = support_root / "verify_bundle.py"
        expected_dependency_lock = support_root / "requirements-win-cp313.txt"
        artifact_lock_path = support_root / "iso02-artifact-lock.json"
        project_metadata_path = support_root / "pyproject.toml"
        bootstrap_script.write_bytes(
            _git_blob(
                repository,
                by_path[
                    "scripts/windows_sandbox/offline_bundle_bootstrap.ps1"
                ].git_object_id,
            )
        )
        verifier_script.write_bytes(
            _git_blob(
                repository,
                by_path["scripts/windows_sandbox/verify_offline_bundle.py"].git_object_id,
            )
        )
        expected_dependency_lock.write_bytes(
            _git_blob(
                repository,
                by_path[
                    "scripts/windows_sandbox/iso02-requirements-win-cp313.txt"
                ].git_object_id,
            )
        )
        artifact_lock_path.write_bytes(artifact_lock_bytes)
        project_metadata_path.write_bytes(project_metadata)
        source = OfflineBundleSourceBinding(
            contract_version=staged.identity.contract_version,
            commit_id=staged.identity.commit_id,
            tree_hash=staged.identity.tree_hash,
            source_input_identity=staged.identity.source_input_identity,
            source_manifest_identity=staged.identity.source_manifest_identity,
            source_archive_sha256=staged.identity.archive_sha256,
        )
        built = build_offline_runtime_bundle(
            OfflineRuntimeBundleBuildRequest(
                destination=arguments.destination,
                source=source,
                python_installer=arguments.python_installer,
                python_version="3.13.7",
                windows_architecture="AMD64",
                wheelhouse=arguments.wheelhouse,
                dependency_roots=dependency_roots,
                project_metadata_sha256=sha256(project_metadata).hexdigest(),
                bootstrap_script=bootstrap_script,
                verifier_script=verifier_script,
                portable_git_root=arguments.portable_git_root,
                reviewed_source_manifest=staged.manifest_path,
                runtime_artifact_lock=artifact_lock_path,
                project_metadata=project_metadata_path,
                portable_git_archive=arguments.portable_git_archive,
                expected_dependency_lock=expected_dependency_lock,
            )
        )
    payload = {
        "bundle_identity": built.manifest.bundle_identity,
        "contract_version": built.manifest.contract_version,
        "dependency_lock_identity": built.manifest.dependency_lock_identity,
        "manifest_sha256": built.manifest_sha256,
        "portable_git_included": built.manifest.portable_git_required,
        "python_runtime_identity": built.manifest.python_runtime_identity,
        "python_version": built.manifest.python_version,
        "source_commit_id": built.manifest.source.commit_id,
        "source_input_identity": built.manifest.source.source_input_identity,
        "wheel_count": len(built.manifest.wheelhouse_inventory),
        "wheelhouse_inventory_identity": built.manifest.wheelhouse_inventory_identity,
    }
    print(
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

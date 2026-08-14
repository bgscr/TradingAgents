"""Verify and reconstruct the reviewed source without importing project code."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zlib
from pathlib import Path


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _git_object(object_format: str, object_type: str, content: bytes) -> tuple[str, bytes]:
    algorithm = "sha1" if object_format == "sha1" else "sha256"
    material = f"{object_type} {len(content)}\0".encode("ascii") + content
    return hashlib.new(algorithm, material).hexdigest(), material


def _install_object(git_dir: Path, object_id: str, material: bytes) -> None:
    destination = git_dir / "objects" / object_id[:2] / object_id[2:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(zlib.compress(material))


def _tree_objects(
    entries: list[dict[str, object]],
    *,
    object_format: str,
    git_dir: Path,
) -> str:
    tree: dict[str, object] = {}
    for entry in entries:
        parts = str(entry["relative_path"]).split("/")
        node = tree
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise RuntimeError("source_tree_collision")
            node = child
        if parts[-1] in node:
            raise RuntimeError("source_tree_collision")
        node[parts[-1]] = entry

    def install_tree(node: dict[str, object]) -> str:
        material: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            encoded_name = name.encode("utf-8")
            if isinstance(value, dict) and "git_object_id" not in value:
                object_id = install_tree(value)
                mode = b"40000"
                sort_key = encoded_name + b"/"
            else:
                entry = value
                if not isinstance(entry, dict):
                    raise RuntimeError("source_manifest_invalid")
                object_id = str(entry["git_object_id"])
                mode = str(entry["git_mode"]).encode("ascii")
                sort_key = encoded_name
            body = mode + b" " + encoded_name + b"\0" + bytes.fromhex(object_id)
            material.append((sort_key, body))
        content = b"".join(value for _key, value in sorted(material, key=lambda item: item[0]))
        object_id, object_material = _git_object(object_format, "tree", content)
        _install_object(git_dir, object_id, object_material)
        return object_id

    return install_tree(tree)


def verify_source(source_input: Path, destination: Path, output: Path) -> dict[str, object]:
    if destination.exists():
        raise RuntimeError("guest_workspace_already_exists")
    manifest_path = source_input / "source-manifest.json"
    archive_path = source_input / "reviewed-source.tar"
    commit_path = source_input / "reviewed-commit.object"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("git_object_format") != "sha1":
        raise RuntimeError("source_object_format_unsupported")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("source_manifest_invalid")
    expected_paths = {str(entry["relative_path"]) for entry in entries}
    if len(expected_paths) != len(entries):
        raise RuntimeError("source_manifest_duplicate")
    destination.mkdir(parents=True)
    by_path = {str(entry["relative_path"]): entry for entry in entries}
    with tarfile.open(archive_path, mode="r:") as archive:
        file_members = [member for member in archive.getmembers() if member.isfile()]
        actual_paths = {member.name for member in file_members}
        if actual_paths != expected_paths:
            raise RuntimeError("source_archive_membership_mismatch")
        for member in archive.getmembers():
            candidate = Path(member.name)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise RuntimeError("source_archive_path_escape")
            if not (member.isfile() or member.isdir()):
                raise RuntimeError("source_archive_entry_type_invalid")
        for member in file_members:
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("source_archive_read_failed")
            content = source.read()
            entry = by_path[member.name]
            if len(content) != int(entry["byte_length"]):
                raise RuntimeError("source_file_length_mismatch")
            if hashlib.sha256(content).hexdigest() != str(entry["sha256"]):
                raise RuntimeError("source_file_digest_mismatch")
            object_id, object_material = _git_object(
                str(manifest["git_object_format"]),
                "blob",
                content,
            )
            if object_id != str(entry["git_object_id"]):
                raise RuntimeError("source_blob_identity_mismatch")
            target = destination / Path(member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    git_dir = destination / ".git"
    (git_dir / "objects").mkdir(parents=True)
    tree_hash = _tree_objects(
        entries,
        object_format=str(manifest["git_object_format"]),
        git_dir=git_dir,
    )
    if tree_hash != str(manifest["tree_hash"]):
        raise RuntimeError("source_tree_identity_mismatch")
    for entry in entries:
        path = destination / Path(str(entry["relative_path"]))
        content = path.read_bytes()
        object_id, object_material = _git_object(
            str(manifest["git_object_format"]),
            "blob",
            content,
        )
        _install_object(git_dir, object_id, object_material)
    commit_content = commit_path.read_bytes()
    commit_id, commit_material = _git_object(
        str(manifest["git_object_format"]),
        "commit",
        commit_content,
    )
    if commit_id != str(manifest["commit_id"]):
        raise RuntimeError("source_commit_identity_mismatch")
    if not commit_content.startswith(f"tree {tree_hash}\n".encode("ascii")):
        raise RuntimeError("source_commit_tree_mismatch")
    _install_object(git_dir, commit_id, commit_material)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "reviewed").write_text(
        commit_id + "\n",
        encoding="ascii",
        newline="\n",
    )
    (git_dir / "HEAD").write_text(
        "ref: refs/heads/reviewed\n",
        encoding="ascii",
        newline="\n",
    )
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = false\n"
        "\tbare = false\n"
        "\tautocrlf = false\n"
        "\thooksPath = NUL\n",
        encoding="ascii",
        newline="\n",
    )
    result = {
        "contract_version": "windows-sandbox-guest-source-verification-v1",
        "commit_id": commit_id,
        "tree_hash": tree_hash,
        "tracked_file_count": len(entries),
        "source_manifest_identity": manifest["source_manifest_identity"],
        "synthetic_git_worktree": True,
        "project_code_imported": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_canonical_json_bytes(result) + b"\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-input", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--verification-output", required=True, type=Path)
    arguments = parser.parse_args()
    verify_source(
        arguments.source_input,
        arguments.destination,
        arguments.verification_output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

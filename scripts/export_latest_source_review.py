"""Export the fixed Issue #7 successor for offline review only, from public Git objects."""

import argparse
import io
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
P = runpy.run_path(str(ROOT / "scripts/propose_ordinary_latest_recipe.py"))
H = P["H"]
REVISION = "144ad68898ede4d3b83d697a6dc15b91f8a2ec64"
ISSUE7 = {
    "tradingagents/dataflows/current_statement_cache.py": "8404c98908de13007b0cfb9379d5dcc45eed9c6cf41312ce24013df1bdb3e18b",
    "tradingagents/dataflows/current_statement_diagnostics.py": "f0fd36b33173db8b67def71883ed400454c6a8054056a0e63e621fe7843a69f3",
}
HELPER_SHA = "666fe86f4aee3dc0f6f2da58d2d1a681ba18b9e7e1e2684c1e9eeaa4d2226e80"
INDEX = "unapproved-latest-144ad688.json"
PUBLIC_MAP_SHA = "6a47251813ba1a70e00b3c6e18a9755233dd5b6b79dc5a1b1e0a19c7f344b4d2"


def parent_recipe():
    raw = H["git"](ROOT, ["cat-file", "blob", REVISION + ":" + H["LOCK"]])
    H["require"](H["sha"](raw.replace(b"\r\n", b"\n")) == H["PIN"], "reviewed_recipe_required")
    return json.loads(raw)


def candidate():
    H["require"](
        Path(H["git"](ROOT, ["rev-parse", "--show-toplevel"]).decode().strip()) == ROOT,
        "exact_source_required",
    )
    base = parent_recipe()
    public = H["public"](base)
    names = sorted([*public, P["NEW_HELPER"]])
    query = "".join(f"{REVISION}:{name}\n" for name in names).encode("ascii")
    stream = io.BytesIO(H["git"](ROOT, ["cat-file", "--batch"], query))
    files = {}
    changes = {}
    for name in names:
        header = stream.readline().split()
        H["require"](len(header) == 3 and header[1] == b"blob", "missing_public_blob")
        data = stream.read(int(header[2]))
        H["require"](stream.read(1) == b"\n", "incomplete_public_blob")
        if name in ISSUE7 or name == P["NEW_HELPER"]:
            expected = ISSUE7.get(name, HELPER_SHA)
            H["require"](H["sha"](data) == expected, "candidate_source_changed")
        elif name in P["CHANGED"]:
            data = P["proposed_source"](name, data, public[name])
        else:
            lf = data.replace(b"\r\n", b"\n")
            data = next(
                (b for b in (data, lf, lf.replace(b"\n", b"\r\n")) if H["sha"](b) == public[name]),
                None,
            )
            H["require"](data is not None, "unexpected_bound_difference")
        if name in public and H["sha"](data) != public[name]:
            changes[name] = {"before": public[name], "proposed": H["sha"](data)}
            public[name] = H["sha"](data)
        files[name] = data
    H["require"](
        not stream.read(1) and set(changes) == P["CHANGED"] | ISSUE7.keys(),
        "unexpected_candidate_delta",
    )
    files[P["INIT"]] = P["INIT_SOURCE"]
    additions = {name: H["sha"](files[name]) for name in (P["INIT"], P["NEW_HELPER"])}
    profile = P["describe_proposal"](base, public, changes, additions)
    H["require"](
        H["sha"](H["canonical"](profile["public_project"])) == PUBLIC_MAP_SHA,
        "public_candidate_mismatch",
    )
    profile.update(contract="ordinary-full-latest-source-review-v2", source_revision=REVISION)
    return profile, files


def export(output):
    output = H["plain"](output)
    H["separate"](ROOT, output)
    H["require"](not output.exists(), "output_exists")
    profile, files = candidate()
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="review-") as tmp:
        folder = Path(tmp) / "candidate"
        for name, data in files.items():
            H["create"](folder / name, data)
        index = H["canonical"](profile)
        H["create"](folder / INDEX, index)
        H["inventory"](folder, {*files, INDEX})
        H["require"](H["R"]["small_file"](folder / INDEX, 2_000_000) == index, "index_changed")
        for name, digest in profile["public_project"].items():
            H["require"](H["R"]["file_hash"](folder / name) == digest, "export_content_changed")
        folder.rename(output)
    return profile


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Fresh absolute review directory")
    args = parser.parse_args()
    try:
        H["require"](
            sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "isolated_python_required",
        )
        metadata = export(args.output)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Review export refused ({type(exc).__name__}).\n")
    print(json.dumps({"status": metadata["status"], "source_revision": REVISION}))

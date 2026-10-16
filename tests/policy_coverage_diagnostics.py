"""Lossy, bounded diagnostics for the synthetic owner fixture's failed capture.

Only existing capture.errors are consumed. No collector, inventory, paths,
exception reprs or environment values enter the diagnostic payload.
"""

import json
import os
import re
from pathlib import Path

_MAX_ERRORS = 16
_MAX_MESSAGE = 4096
_OUTPUT_ENV = "TA_CI_POLICY_COVERAGE_DIAGNOSTICS"

# Exact static messages from policy_coverage_native/contract. Never use substring
# matches: a message with an appended path or secret must become unknown.
_KNOWN_GUARDS = frozenset(
    {
        "coverage acquisition requires an existing local fixed volume",
        "offline/recall objects require forbidden hydration",
        "reparse/aliased/unsupported ordinary measurement object",
        "opened measurement object changed type/identity/availability",
        "held ordinary security descriptor changed during acquisition",
        "held measurement object changed during acquisition",
        "finite ordinary ancestor depth exhausted",
        "native opened object differs from named observation",
        "ordinary security descriptor changed before observation",
        "declared link is not a reparse object",
        "opened link type/alias mismatch",
        "link object or reparse bytes changed during held acquisition",
        "native containment depth exceeds contract",
        "indirect/cyclic/aliased containment component is forbidden",
        "bounded local Windows measurement required",
        "link is outside exact measurement root",
        "reparse target escapes exact protected root",
        "reparse target cycle",
        "link containment spans a different volume",
        "native link target cycle",
        "finite capture time exhausted",
        "finite capture entry/byte budget exhausted",
        "finite directory enumeration entry budget exhausted",
        "finite capture depth exhausted",
        "actual ordinary root type differs from trusted scope",
        "new or undeclared reparse object",
        "directory membership changed during capture",
        "file length changed during capture",
        "source changed during capture",
        "ambiguous relative evidence path",
        "ambiguous relative evidence component",
        "unambiguous local drive path required",
        "noncanonical local evidence path",
        "reparse buffer length outside contract",
        "unsupported reparse tag or inconsistent buffer length",
        "invalid reparse name offsets",
        "invalid reparse name encoding",
        "unsupported reparse target namespace",
        "ambiguous reparse display/substitute target",
        "complete native object/volume identity required",
        "declared current namespace has a reparse cycle",
        "current version/scope/measurement/candidate/instance binding mismatch",
        "current inventory canonical identity mismatch",
        "current inventory omits or changes declared protected roots",
        "incomplete current root or root identity mismatch",
        "current paths duplicated, aliased, missing or unordered",
        "complete ordinary root row required",
        "exact file root must contain only its ordinary root row",
        "current directory depth exceeds finite contract",
        "current path parent is missing or an alias",
        "unreadable, missing, unsupported or incomplete current row",
        "required current raw/security coverage missing",
        "current raw attribute/kind contradiction",
        "complete current file byte pin required",
        "new, undeclared or unsupported reparse object",
        "invalid reparse buffer encoding",
        "reparse bytes exceed measurement bound",
        "reparse target relationship differs from raw buffer",
        "dangling, cyclic, indirect or unmeasured reparse target",
        "reparse containment object/volume proof mismatch",
        "current complete file count mismatch",
        "current inventory contains an object alias",
        "current acquisition completeness/method/time/provenance mismatch",
    }
)
_OS_CODE = re.compile(r"\A\[(WinError|Errno) (-?[0-9]{1,10})\](?: |$)")


def _safe_error(error):
    result = {"stage": "unknown", "message_kind": "invalid"}
    if type(error) is not dict:
        return result
    for key in ("capture_stopped", "complete_inventory_validation", "collector", "error"):
        if key in error:
            result["stage"] = key
            if key == "error":
                result["stage"] = "entry" if "path" in error else "root_or_ancestor"
            message = error[key]
            break
    else:
        return result
    if type(message) is not str:
        return result
    if len(message) > _MAX_MESSAGE:
        result["message_kind"] = "oversized"
    elif message in _KNOWN_GUARDS:
        result.update(message_kind="known_guard", guard=message)
    else:
        match = _OS_CODE.match(message)
        if match and -(2**31) <= int(match[2]) <= 2**32 - 1:
            result.update(message_kind=match[1], code=int(match[2]))
        else:
            result["message_kind"] = "unknown"
    return result


def capture_failure_diagnostic(errors):
    """Return safe assertion text; append only that projection when CI opts in.

    The caller uses this as a lazy assertion message. Logging failures cannot
    replace the original completeness failure or expose an OS exception path.
    """
    valid = type(errors) is list
    payload = {
        "schema": "synthetic-owner-capture-diagnostic-v1",
        "fixture": "owner_case",
        "phase": "before",
        "errors_state": "present" if valid and errors else "empty" if valid else "invalid",
        "error_count": len(errors) if valid else None,
        "omitted_errors": max(0, len(errors) - _MAX_ERRORS) if valid else None,
        "errors": [_safe_error(error) for error in errors[:_MAX_ERRORS]] if valid else [],
        "artifact_status": "disabled",
    }
    output = os.environ.get(_OUTPUT_ENV)
    if output:
        payload["artifact_status"] = "written"
        try:
            with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
        except (OSError, ValueError):
            payload["artifact_status"] = "unavailable"
    return json.dumps(payload, sort_keys=True)

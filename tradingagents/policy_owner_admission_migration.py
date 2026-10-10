"""Explicit same-instance maintenance coordinator; importing performs no I/O.

This is an executable FUTURE platform path, not evidence that this host has
qualified its boot/SCM/ACL adapter. Missing protected authorization, qualified
platform evidence, or a complete fresh-boot cut fails closed. It never stops or
starts a service, reboots, authenticates, restores v1, repairs ACLs, or cleans up
partial evidence. Existing owner metadata/immutable rows are not rewritten.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import secrets
import sys
import time

from tradingagents.policy_owner_admission_v2 import (
    EnrollmentOnlyStore, PlatformValidationRequired, StorePaths, canonical,
    classify_legacy_copies, closed, identity, integer, pin, require, seal,
    strict_json, utc, validate_binding, validate_pin, validate_role,
)
from tradingagents.policy_owner_admission_windows import WindowsAuthority, _FileInfo

MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_COPY_MEMBERS = 10000
STEPS = {"prepare_guard", "post_boot"}


class _SA(c.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]


class _ServiceConfig(c.Structure):
    _fields_ = [("service_type", w.DWORD), ("start", w.DWORD), ("error_control", w.DWORD),
                ("binary", w.LPWSTR), ("group", w.LPWSTR), ("tag", w.DWORD),
                ("dependencies", w.LPWSTR), ("account", w.LPWSTR), ("display", w.LPWSTR)]


class _FailureActions(c.Structure):
    _fields_ = [("reset", w.DWORD), ("reboot", w.LPWSTR), ("command", w.LPWSTR),
                ("count", w.DWORD), ("actions", c.c_void_p)]


class _TriggerInfo(c.Structure):
    _fields_ = [("count", w.DWORD), ("triggers", c.c_void_p), ("reserved", c.c_void_p)]


class MaintenanceCoordinator:
    """Small protocol surface also exercised by a separate synthetic fake."""
    def __init__(self, backend, *, synthetic_protocol_test: bool = False) -> None:
        require(isinstance(backend, WindowsMaintenanceAuthority) or
                (synthetic_protocol_test and backend.context == "SYNTHETIC_PROTOCOL_TEST"),
                "actual Windows maintenance capability required")
        self.backend = backend

    def run(self, step: str) -> dict:
        require(step in STEPS, "maintenance step")
        backend = self.backend
        primary_error = None
        try:
            backend.validate_authorization(step)
            if step == "prepare_guard":
                backend.prepare_control_new()
                backend.acquire_gate()
                old_boot = backend.observe_boot()
                original_startup = backend.observe_legacy_startup()
                backend.journal("GUARD_CHANGE_INTENT", {"old_boot_id": old_boot, "original_startup": original_startup})
                backend.change_startup(4)
                observed = backend.observe_startup(4)
                guard = backend.save_guard(old_boot, observed)
                backend.journal("GUARD_ESTABLISHED", {"guard_identity": guard["identity"]})
                return {"status": "GUARD_ESTABLISHED_STOP_FOR_SEPARATE_FULL_KERNEL_BOOT_AUTHORIZATION",
                        "context": backend.context, "guard_identity": guard["identity"],
                        "service_stopped": False, "service_started": False, "reboot_performed": False,
                        "authentication_performed": False, "execution_authorized": False}
            backend.open_control_existing()
            backend.acquire_gate()
            guard = backend.load_guard()
            new_boot = backend.observe_boot()
            require(new_boot != guard["old_boot_id"], "same kernel boot cannot establish the cut")
            observed = backend.observe_startup(4)
            backend.journal("POST_BOOT_CLASSIFICATION_INTENT", {"old_boot_id": guard["old_boot_id"], "new_boot_id": new_boot})
            boot = backend.save_boot(guard, new_boot, observed)
            copies = backend.capture_copies(guard, boot)
            classification = backend.classify(copies)
            backend.save_classification(classification)
            if classification.kind != "ELIGIBLE":
                return {"status": "STOP_" + classification.kind + "_NO_OWNER_DATABASE_OR_BINDING_MIGRATION",
                        "context": backend.context, "classification": classification.evidence,
                        "startup_remains_disabled": True, "execution_authorized": False}
            backend.release_copy_fences()
            store = backend.store()
            migrated = store.migrate_closed(classification)
            backend.journal("SCHEMA_MIGRATED_CLOSED", migrated)
            backend.stage_registration()
            staged = store.stage_binding()
            backend.journal("NEW_BINDING_STAGED", staged)
            backend.verify_registration()
            activated = store.activate_closed()
            backend.journal("ACTIVATED_CLOSED_STARTUP_CHANGE_INTENT", activated)
            backend.change_startup(3)
            observed = backend.observe_startup(3)
            backend.journal("ACTIVATION_PUBLICATION_INTENT", {"activation": activated, "startup": observed})
            activation = backend.finalize_activation(observed)
            # If this final observational journal fails, activation may already
            # be committed; report UNKNOWN and never retry/reserve automatically.
            backend.journal("V2_ACTIVATED_CLOSED", {"activation_identity": activation["identity"]})
            return {"status": "V2_ACTIVATED_CLOSED_STOP_FOR_SEPARATE_HUMAN_DECISION",
                    "context": backend.context, "gate": "CLOSED", "service_started": False,
                    "authentication_performed": False, "legacy_fallback": False, "execution_authorized": False}
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                backend.close()
            except BaseException as release_error:
                if primary_error is None:
                    raise
                raise BaseExceptionGroup(
                    "maintenance operation and owned-handle release both failed",
                    [primary_error, release_error],
                )


class WindowsMaintenanceAuthority(WindowsAuthority):
    """Administrator operator, own gate/lease, measured SCM and fresh-kernel cut."""
    context = "PRODUCTION_MAINTENANCE"

    def __init__(self, instance: str, step: str, confirmation: str, depot: Path) -> None:
        require(os.name == "nt" and c.sizeof(c.c_void_p) == 8 and
                sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
                "isolated Windows x64 maintenance interpreter required")
        require(instance == "operational-20260930-001" and step in STEPS, "fixed same-instance maintenance only")
        self.started = time.monotonic()
        self.deadline = self.started + 900
        self.step, self.confirmation = step, confirmation
        self.legacy = {"instance": instance, "service_sid": ""}
        self.held = []
        self._opened = {}
        self.copy_fences: list[int] = []
        self.service_handles: list[int] = []
        self._lease = secrets.token_hex(32)
        self._gate_handle = None
        self._prepared_gate = None
        self._lease_handle = None
        self._lease_bytes = None
        self._cut_ready = False
        self._physical_cut_ready = False
        self._expected_start = 4
        self._staged = False
        self._owner_sql_authorized = False
        self._write_key_handle = None
        self.cut = None
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.a = c.WinDLL("advapi32", use_last_error=True)
        self._declare()
        self._declare_maintenance()
        try:
            staging = self._registry_json("AdmissionV2StagingJson")
            closed(staging, "contract_version binding depot_path cut_record_path cut_record_pin platform_profile_path platform_profile_pin copies_root baseline_pins activation_record_path activation_record_pin", "protected staging registration")
            require(staging["contract_version"] == "windows-policy-owner-admission-anchor-v2", "staging version")
            require(staging["cut_record_pin"] is None, "staging must explicitly leave the not-yet-produced cut pin UNKNOWN/null")
            require(staging["activation_record_pin"] is None, "staging cannot claim a future activation receipt")
            self.binding = validate_binding(staging["binding"])
            self.legacy = self.binding["legacy_setup_binding"]
            self.root = Path(self.legacy["owner_root"])
            self.depot = depot.absolute()
            self.control = self.root / "state" / "admission-v2"
            require(Path(staging["activation_record_path"]) == self.control / "activation.json", "fixed new activation path")
            require(self.legacy["instance"] == instance and self.root == Path(r"C:\ProgramData\TA-PolicyOwner-" + instance) and
                    self.depot == self.root / "admission-v2" == Path(staging["depot_path"]), "same-instance fixed depot/root")
            require(re.fullmatch(r"S-1-5-80-(?:[0-9]+-){4}[0-9]+", self.legacy["service_sid"]), "dedicated service SID")
            self.registration = staging
            self._require_operator_admin()
            self.authorization = self._registry_json("AdmissionV2MaintenanceAuthorization")
            self.validate_authorization(step)
            self._hold_legacy()
            self._hold_depot()
            self.profile = self._pinned_json(staging["platform_profile_path"], staging["platform_profile_pin"])
            self._validate_platform_report()
            self.role_raw = (self.root / "role-proposal.json").read_bytes()
            validate_role(self.role_raw)
            self._service = self._open_service()
            if self.step == "prepare_guard":
                self.observe_legacy_startup()
            else:
                self.observe_startup(4)
            self._install_audit()
        except BaseException as error:
            try:
                self.close()
            except BaseException as release_error:
                raise BaseExceptionGroup(
                    "maintenance authority construction and release both failed",
                    [error, release_error],
                )
            raise

    def _budget(self) -> None:
        require(time.monotonic() < self.deadline, "maintenance cooperative budget exhausted")
        if hasattr(self, "authorization"):
            require(datetime.now(timezone.utc) < utc(self.authorization["expires_utc"]), "maintenance authorization expired during step")

    def _install_audit(self) -> None:
        def audit(event: str, values: tuple) -> None:
            if event in {"subprocess.Popen", "os.system", "os.startfile", "os.startfile/2",
                         "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
                         "sqlite3.enable_load_extension", "sqlite3.load_extension",
                         "winreg.CreateKey", "winreg.DeleteKey", "winreg.DeleteValue",
                         "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.chmod"}:
                raise PlatformValidationRequired("maintenance audit rejects effect: " + event)
            if event == "winreg.SetValue":
                require(self._write_key_handle is not None and int(values[0]) == self._write_key_handle and
                        values[1] == "AdmissionV2Json", "only exact newly staged v2 registry value is writable")
            if event == "sqlite3.connect":
                from urllib.parse import unquote, urlsplit, parse_qs
                value = str(values[0])
                require(value.startswith("file:"), "explicit SQLite URI required")
                parsed = urlsplit(value)
                require(not parsed.netloc, "local SQLite only")
                local = unquote(parsed.path)
                if re.match(r"^/[A-Za-z]:", local):
                    local = local[1:]
                target, query = Path(local), parse_qs(parsed.query)
                if target == self.root / "state" / "owner.sqlite3":
                    require(self._owner_sql_authorized and query == {"mode": ["rw"]}, "owner SQL only after eligible cut")
                else:
                    require(target.is_relative_to(self.control / "copies") and
                            query == {"mode": ["ro"], "immutable": ["1"]}, "SQLite reads only on complete new copies")
            if event == "open" and isinstance(values[0], (str, bytes, os.PathLike)):
                if values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    target = Path(os.fsdecode(values[0])).absolute()
                    require(".." not in target.parts and target.is_relative_to(self.control),
                            "Python writes restricted to new maintenance control evidence")
            if event == "os.mkdir":
                require(Path(values[0]).absolute().is_relative_to(self.control), "new directories only in maintenance control")
        sys.addaudithook(audit)

    def _declare_maintenance(self) -> None:
        self.k.CreateDirectoryW.argtypes = [w.LPCWSTR, c.POINTER(_SA)]
        self.k.WriteFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        self.k.FlushFileBuffers.argtypes = [w.HANDLE]
        self.k.GetCurrentProcessId.restype = w.DWORD
        self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]
        self.a.CheckTokenMembership.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(w.BOOL)]
        self.a.CreateWellKnownSid.argtypes = [c.c_int, c.c_void_p, c.c_void_p, c.POINTER(w.DWORD)]
        self.a.OpenSCManagerW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
        self.a.OpenSCManagerW.restype = w.HANDLE
        self.a.OpenServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.DWORD]
        self.a.OpenServiceW.restype = w.HANDLE
        self.a.CloseServiceHandle.argtypes = [w.HANDLE]
        self.a.QueryServiceConfigW.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        self.a.QueryServiceConfig2W.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        self.a.QueryServiceObjectSecurity.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        self.a.ChangeServiceConfigW.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD,
            w.LPCWSTR, w.LPCWSTR, c.POINTER(w.DWORD), w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR]
        self.a.GetSecurityDescriptorGroup.argtypes = [c.c_void_p, c.POINTER(c.c_void_p), c.POINTER(w.BOOL)]
        self.a.GetSecurityDescriptorControl.argtypes = [c.c_void_p, c.POINTER(w.WORD), c.POINTER(w.DWORD)]

    def _open(self, path: Path, *, data: bool = False, immutable: bool = False):
        self._budget()
        return super()._open(path, data=data, immutable=immutable)

    def _require_operator_admin(self) -> None:
        require(self._token_sid(self.k.GetCurrentProcess()) == self.legacy["operator_sid"], "specified operator token required")
        token = w.HANDLE()
        self._ok(self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, c.byref(token)))
        try:
            elevation, needed = w.DWORD(), w.DWORD()
            self._ok(self.a.GetTokenInformation(token, 20, c.byref(elevation), c.sizeof(elevation), c.byref(needed)))
            sid, size, member = c.create_string_buffer(68), w.DWORD(68), w.BOOL()
            self._ok(self.a.CreateWellKnownSid(26, None, sid, c.byref(size)))
            self._ok(self.a.CheckTokenMembership(None, sid, c.byref(member)))
            require(elevation.value == 1 and member.value, "already elevated specified administrator required; no elevation is attempted")
        finally:
            self.k.CloseHandle(token)

    def validate_authorization(self, step: str) -> None:
        self._budget()
        authorization = self.authorization
        closed(authorization, "contract_version binding_identity step operator_sid expires_utc budget_seconds authorizing_document_path authorizing_document_pin identity", "maintenance authorization")
        require(authorization["identity"] == identity("policy-owner-admission-maintenance-authorization:v2",
                    {k: v for k, v in authorization.items() if k != "identity"}) and
                authorization["contract_version"] == "policy-owner-admission-maintenance-authorization-v2" and
                authorization["binding_identity"] == self.binding["identity"] and authorization["step"] == step == self.step and
                authorization["operator_sid"] == self.legacy["operator_sid"] and
                self.confirmation == authorization["identity"], "exact protected authorization/confirmation mismatch")
        require(datetime.now(timezone.utc) < utc(authorization["expires_utc"]), "maintenance authorization expired")
        budget = integer(authorization["budget_seconds"], 900)
        require(budget > 0, "explicit maintenance budget required")
        self.deadline = self.started + budget
        path = Path(authorization["authorizing_document_path"])
        self._protect_path(path, immutable=True)
        require(pin(path.read_bytes()) == validate_pin(authorization["authorizing_document_pin"]), "authorizing document bytes changed")
        self._budget()

    def _hold_legacy(self) -> None:
        self._anchor_ancestors()
        anchor = self._registry_json("AnchorJson")
        closed(anchor, "contract_version instance operational root root_object_id root_descriptor_sha256 service_sid operator_sid native_path native_sha256 worker_manifest_sha256 setup_proposal_sha256 original_registry_sha256 ledger_object_id owner_state_object_id protected_directories ledger_identity storage_binding_identity", "original owner anchor")
        require(anchor["storage_binding_identity"] == self.legacy["identity"] and anchor["root"] == str(self.root) and
                anchor["operator_sid"] == self.legacy["operator_sid"] and anchor["service_sid"] == self.legacy["service_sid"] and
                anchor["ledger_identity"] == self.legacy["ledger_identity"], "original AnchorJson binding changed")
        require(anchor["contract_version"] == "windows-policy-owner-anchor-v1" and anchor["instance"] == self.legacy["instance"] and
                anchor["operational"] is True and anchor["root_object_id"] == self.legacy["owner_root_object_id"] and
                anchor["root_descriptor_sha256"] == self.legacy["owner_root_descriptor_pin"]["sha256"] and
                anchor["owner_state_object_id"] == self.legacy["owner_state_object_id"] and
                anchor["ledger_object_id"] == self.legacy["ledger_object_id"] and
                anchor["native_path"] == str(self.root / "bin" / "PolicyOwner.exe") and
                anchor["native_sha256"] == self.legacy["native_owner_pin"]["sha256"] and
                anchor["worker_manifest_sha256"] == self.legacy["trusted_worker_manifest_pin"]["sha256"] and
                anchor["original_registry_sha256"] == self.legacy["original_registry_pin"]["sha256"], "original immutable anchor fields differ")
        for path, field in ((self.root, "owner_root_object_id"), (self.root / "state" / "owner.sqlite3", "owner_state_object_id"),
                            (self.root / "state" / "lineage.sqlite3", "ledger_object_id")):
            _, info, descriptor = self._open(path)
            require(f"{info.volume:08x}:{info.index_high:08x}{info.index_low:08x}" == self.legacy[field], "original object replaced")
            if path == self.root:
                require(pin(descriptor) == self.legacy["owner_root_descriptor_pin"], "original root permissions changed")
        for name, expected in (("bin/PolicyOwner.exe", self.legacy["native_owner_pin"]),
                               ("worker-manifest.json", self.legacy["trusted_worker_manifest_pin"]),
                               ("original-acquisitions.json", self.legacy["original_registry_pin"])):
            path = self.root / name
            self._protect_path(path, immutable=True)
            require(pin(path.read_bytes()) == expected, "original immutable input changed")
        self._protect_path(self.root / "role-proposal.json", immutable=True)
        names = set()
        for item in anchor["protected_directories"]:
            closed(item, "relative_path object_id descriptor_sha256", "original protected directory")
            require(item["relative_path"] in {"state", "state/temp", "authentication", "artifacts"} and
                    item["relative_path"] not in names, "unknown/duplicate original protected directory")
            names.add(item["relative_path"])
            _, info, descriptor = self._open(self.root / item["relative_path"])
            require(f"{info.volume:08x}:{info.index_high:08x}{info.index_low:08x}" == item["object_id"] and
                    pin(descriptor)["sha256"] == item["descriptor_sha256"], "original directory binding changed")
        require(names == {"state", "state/temp", "authentication", "artifacts"}, "incomplete original directory bindings")

    def _open_service(self) -> int:
        manager = self.a.OpenSCManagerW(None, None, 1)
        self._ok(manager)
        self.service_handles.append(manager)
        service = self.a.OpenServiceW(manager, self.legacy["service_name"], 0x20000 | 1 | 2)
        self._ok(service)
        self.service_handles.append(service)
        self._service_permissions(service)
        return service

    def _service_permissions(self, service: int) -> None:
        needed = w.DWORD()
        self.a.QueryServiceObjectSecurity(service, 5, None, 0, c.byref(needed))
        require(0 < needed.value <= 65536, "bounded service descriptor")
        raw = c.create_string_buffer(needed.value)
        self._ok(self.a.QueryServiceObjectSecurity(service, 5, raw, needed, c.byref(needed)))
        owner, acl, default, present = c.c_void_p(), c.c_void_p(), w.BOOL(), w.BOOL()
        self._ok(self.a.GetSecurityDescriptorOwner(raw, c.byref(owner), c.byref(default)))
        require(self._sid(owner.value) in {"S-1-5-18", "S-1-5-32-544"}, "service config owner")
        self._ok(self.a.GetSecurityDescriptorDacl(raw, c.byref(present), c.byref(acl), c.byref(default)))
        require(present.value and acl.value, "service DACL required")
        count = c.c_ushort.from_address(acl.value + 4).value
        for index in range(count):
            ace = c.c_void_p()
            self._ok(self.a.GetAce(acl, index, c.byref(ace)))
            kind = c.c_ubyte.from_address(ace.value).value
            require(kind in (0, 1), "service unsupported ACE")
            mask = c.c_uint32.from_address(ace.value + 4).value
            if kind == 0 and mask & 0x500D0002:
                require(self._sid(ace.value + 8) in {"S-1-5-18", "S-1-5-32-544"}, "untrusted service configuration mutation rights")

    def _config2(self, kind: int) -> c.Array:
        needed = w.DWORD()
        self.a.QueryServiceConfig2W(self._service, kind, None, 0, c.byref(needed))
        minimum = {2: c.sizeof(_FailureActions), 3: 4, 8: c.sizeof(_TriggerInfo)}[kind]
        require(minimum <= needed.value <= 65536, "bounded service configuration extension")
        raw = c.create_string_buffer(needed.value)
        self._ok(self.a.QueryServiceConfig2W(self._service, kind, raw, needed, c.byref(needed)))
        return raw

    def _startup_policy(self) -> dict:
        self._budget()
        self._service_permissions(self._service)
        needed = w.DWORD()
        self.a.QueryServiceConfigW(self._service, None, 0, c.byref(needed))
        require(c.sizeof(_ServiceConfig) <= needed.value <= 65536, "bounded QueryServiceConfig")
        raw = c.create_string_buffer(needed.value)
        self._ok(self.a.QueryServiceConfigW(self._service, raw, needed, c.byref(needed)))
        config = c.cast(raw, c.POINTER(_ServiceConfig)).contents
        require(config.service_type == 16 and config.account == "NT SERVICE\\" + self.legacy["service_name"],
                "existing service account/type must be preserved")
        failure_raw = self._config2(2)
        failure = c.cast(failure_raw, c.POINTER(_FailureActions)).contents
        require(not failure.command and not failure.reboot and failure.count == 0, "unqualified recovery actions/command")
        delayed = c.c_uint32.from_buffer(self._config2(3)).value
        trigger_raw = self._config2(8)
        triggers = c.cast(trigger_raw, c.POINTER(_TriggerInfo)).contents
        require(delayed == 0 and triggers.count == 0, "unqualified delayed/trigger startup")
        return {"image_path": config.binary, "start": config.start, "type": config.service_type,
                "account": config.account, "failure_command": "", "delayed_auto_start": 0, "trigger_info": None}

    def observe_startup(self, expected_start: int) -> dict:
        observed = self._startup_policy()
        require(observed == {"image_path": self.new_image_command, "start": expected_start, "type": 16,
            "account": "NT SERVICE\\" + self.legacy["service_name"], "failure_command": "", "delayed_auto_start": 0,
            "trigger_info": None}, "measured startup guard readback mismatch")
        return observed

    def observe_legacy_startup(self) -> dict:
        observed = self._startup_policy()
        expected = {"image_path": '"' + str(self.root / "bin" / "PolicyOwner.exe") + '" service ' + self.legacy["instance"],
                    "start": 3, "type": 16, "account": "NT SERVICE\\" + self.legacy["service_name"],
                    "failure_command": "", "delayed_auto_start": 0, "trigger_info": None}
        require(observed == expected, "original manual-start v1 configuration differs; unknown configuration is not repaired")
        return observed

    @property
    def new_image_command(self) -> str:
        return '"' + str(self.depot / "bin" / "PolicyOwner.exe") + '" service-v2 ' + self.legacy["instance"]

    def change_startup(self, start: int) -> None:
        require(start in {3, 4}, "only disabled/new-demand startup allowed")
        self.validate_authorization(self.step)
        require(self._gate_handle is not None and self._lease_handle is not None, "own exclusive maintenance gate required")
        if start == 4:
            require(self.step == "prepare_guard", "only initial guard step may replace exact original configuration")
            self.observe_legacy_startup()
        else:
            require(self.step == "post_boot" and self._staged and self._cut_ready,
                    "new manual startup only after a fully closed activation")
            require(self.store().inspect()["migration_phase"] == "V2_ACTIVATED_CLOSED", "durable closed activation missing")
            self.observe_startup(4)
        self._ok(self.a.ChangeServiceConfigW(self._service, 0xFFFFFFFF, start, 0xFFFFFFFF,
            self.new_image_command, None, None, None, None, None, None))
        self._expected_start = start
        self.observe_startup(start)

    def _attributes(self, directory: bool) -> tuple[_SA, c.c_void_p]:
        flags = "OICI" if directory else ""
        sddl = (f"O:BAG:BAD:P(D;{flags};WDWO;;;OW)(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"
                f"(A;{flags};FA;;;{self.legacy['service_sid']})(A;{flags};FRFX;;;{self.legacy['operator_sid']})")
        descriptor = c.c_void_p()
        self._ok(self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
        return _SA(c.sizeof(_SA), descriptor, False), descriptor

    def _verify_new_security(self, handle: int, *, directory: bool) -> None:
        raw = c.create_string_buffer(self._descriptor(handle))
        owner, group, acl = c.c_void_p(), c.c_void_p(), c.c_void_p()
        default, present, control, revision = w.BOOL(), w.BOOL(), w.WORD(), w.DWORD()
        self._ok(self.a.GetSecurityDescriptorOwner(raw, c.byref(owner), c.byref(default)))
        self._ok(self.a.GetSecurityDescriptorGroup(raw, c.byref(group), c.byref(default)))
        self._ok(self.a.GetSecurityDescriptorControl(raw, c.byref(control), c.byref(revision)))
        require(self._sid(owner.value) == self._sid(group.value) == "S-1-5-32-544" and control.value & 0x1000,
                "new control object owner/group/protected DACL differs")
        self._ok(self.a.GetSecurityDescriptorDacl(raw, c.byref(present), c.byref(acl), c.byref(default)))
        require(present.value and acl.value and c.c_ushort.from_address(acl.value + 4).value == 5,
                "new control full DACL shape differs")
        expected = [(1, 0x000C0000, "S-1-3-4"), (0, 0x001F01FF, "S-1-5-18"),
                    (0, 0x001F01FF, "S-1-5-32-544"), (0, 0x001F01FF, self.legacy["service_sid"]),
                    (0, 0x001200A9, self.legacy["operator_sid"])]
        for index, (kind, mask, sid) in enumerate(expected):
            ace = c.c_void_p()
            self._ok(self.a.GetAce(acl, index, c.byref(ace)))
            require(c.c_ubyte.from_address(ace.value).value == kind and
                    c.c_ubyte.from_address(ace.value + 1).value == (3 if directory else 0) and
                    c.c_uint32.from_address(ace.value + 4).value == mask and self._sid(ace.value + 8) == sid,
                    "new control complete DACL readback mismatch")

    def _mkdir_new(self, path: Path) -> None:
        self._budget()
        require(path.is_relative_to(self.control), "new maintenance directory outside control root")
        attributes, descriptor = self._attributes(True)
        try:
            self._ok(self.k.CreateDirectoryW(str(path), c.byref(attributes)))
        finally:
            self.k.LocalFree(descriptor)
        handle, _, _ = self._open(path)
        self._verify_new_security(handle, directory=True)

    def _new_bytes(self, path: Path, raw: bytes, *, keep_write: bool = False) -> int:
        self._budget()
        require(path.is_relative_to(self.control), "new maintenance output outside control root")
        attributes, descriptor = self._attributes(False)
        try:
            handle = self.k.CreateFileW(str(path), 0xC0020080, 1, c.byref(attributes), 1, 0x00200000, None)
        finally:
            self.k.LocalFree(descriptor)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        self.held.append(handle)
        self._verify_new_security(handle, directory=False)
        try:
            for offset in range(0, len(raw), 1024 * 1024):
                self._budget()
                chunk = raw[offset:offset + 1024 * 1024]
                buffer, written = c.create_string_buffer(chunk), w.DWORD()
                self._ok(self.k.WriteFile(handle, buffer, len(chunk), c.byref(written), None))
                require(written.value == len(chunk), "short maintenance output write")
            self._ok(self.k.FlushFileBuffers(handle))
            require(path.read_bytes() == raw, "maintenance output readback changed")
        except BaseException:
            raise  # Preserve bytes and handle until ordinary finally release.
        if not keep_write:
            self._ok(self.k.CloseHandle(handle))
            self.held.remove(handle)
            self._protect_path(path, immutable=True)
        return handle

    def journal(self, phase: str, evidence: dict) -> dict:
        require(re.fullmatch("[A-Z0-9_]+", phase), "journal phase")
        value = seal("policy-owner-admission-maintenance-journal:v2", {
            "contract_version": "policy-owner-admission-maintenance-journal-v2", "binding_identity": self.binding["identity"],
            "authorization_identity": self.authorization["identity"], "step": self.step, "phase": phase,
            "lease_id": self._lease, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence, "execution_authorized": False})
        self._new_bytes(self.control / (self.step + "-" + phase + ".json"), canonical(value))
        return value

    def prepare_control_new(self) -> None:
        self._mkdir_new(self.control)  # Existing/unknown directory is never repaired or adopted.
        self._prepared_gate = self._new_bytes(self.control / "owner-gate.lock", b"ADMISSION_V2_EXCLUSIVE_GATE\n", keep_write=True)

    def open_control_existing(self) -> None:
        handle, _, _ = self._open(self.control)
        self._verify_new_security(handle, directory=True)
        self._protect_path(self.control / "owner-gate.lock", immutable=False)

    def acquire_gate(self) -> None:
        self._budget()
        gate = self._prepared_gate
        if gate is None:
            gate = self.k.CreateFileW(str(self.control / "owner-gate.lock"), 0xC0020080, 1, None, 3, 0x00200000, None)
            if gate == c.c_void_p(-1).value:
                raise c.WinError(c.get_last_error())
            self.held.append(gate)
        self._gate_handle = gate
        self._verify_new_security(gate, directory=False)
        creation, exited, kernel, user = (c.c_uint64() for _ in range(4))
        self._ok(self.k.GetProcessTimes(self.k.GetCurrentProcess(), c.byref(creation), c.byref(exited), c.byref(kernel), c.byref(user)))
        holder = {"pid": self.k.GetCurrentProcessId(), "creation_filetime": creation.value,
                  "image_pin": pin(Path(sys.executable).read_bytes())}
        lease = {"contract_version": "policy-owner-admission-lease-v2", "lease_id": self._lease,
                 "binding_identity": self.binding["identity"], "holder": holder,
                 "holder_handles": {"gate": int(gate), "lease": 0}}
        # The lease's own raw HANDLE is needed by the later service's independent
        # handoff evidence. Reserve it first, then write one canonical final body.
        attributes, descriptor = self._attributes(False)
        path = self.control / ("lease-" + self._lease + ".json")
        try:
            handle = self.k.CreateFileW(str(path), 0xC0020080, 1, c.byref(attributes), 1, 0x00200000, None)
        finally:
            self.k.LocalFree(descriptor)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        self.held.append(handle)
        self._lease_handle = handle
        self._verify_new_security(handle, directory=False)
        lease["holder_handles"]["lease"] = int(handle)
        raw = canonical(lease)
        written, buffer = w.DWORD(), c.create_string_buffer(raw)
        self._ok(self.k.WriteFile(handle, buffer, len(raw), c.byref(written), None))
        require(written.value == len(raw), "short held lease write")
        self._ok(self.k.FlushFileBuffers(handle))
        require(path.read_bytes() == raw, "held lease readback")
        self._lease_bytes = raw

    def observe_boot(self) -> str:
        self._budget()
        return self._boot_identity()

    def save_guard(self, old_boot: str, startup: dict) -> dict:
        guard = seal("policy-owner-admission-startup-guard:v2", {
            "contract_version": "policy-owner-admission-startup-guard-v2", "binding_identity": self.binding["identity"],
            "legacy_binding_identity": self.legacy["identity"], "profile_identity": self.profile["identity"],
            "old_boot_id": old_boot, "disabled_startup": startup, "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
        self._new_bytes(self.control / "guard.json", canonical(guard))
        return guard

    def load_guard(self) -> dict:
        guard = self._protected_json(self.control / "guard.json")
        closed(guard, "contract_version binding_identity legacy_binding_identity profile_identity old_boot_id disabled_startup recorded_at_utc identity", "existing raw guard")
        require(guard["identity"] == identity("policy-owner-admission-startup-guard:v2", {k: v for k, v in guard.items() if k != "identity"}) and
                guard["contract_version"] == "policy-owner-admission-startup-guard-v2" and
                guard["binding_identity"] == self.binding["identity"] and guard["legacy_binding_identity"] == self.legacy["identity"] and
                guard["profile_identity"] == self.profile["identity"], "raw guard/proposal/profile mismatch")
        return guard

    def save_boot(self, guard: dict, new_boot: str, startup: dict) -> dict:
        require(new_boot != guard["old_boot_id"] and guard["disabled_startup"] == startup, "fresh boot preserves disabled startup")
        boot = seal("policy-owner-admission-fresh-boot:v2", {"contract_version": "policy-owner-admission-fresh-boot-v2",
            "binding_identity": self.binding["identity"], "guard_identity": guard["identity"], "new_boot_id": new_boot,
            "disabled_startup": startup, "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
        self._new_bytes(self.control / "boot.json", canonical(boot))
        return boot

    def _copy(self, source: Path, target: Path, *, maximum: int = 256 * 1024 * 1024) -> dict:
        self._budget()
        self._protect_path(source, immutable=True)
        size = source.stat().st_size
        require(0 <= size <= maximum, "capture member capacity")
        raw = source.read_bytes()
        require(len(raw) == size, "capture source size changed")
        self._new_bytes(target, raw)
        return {"relative_path": target.relative_to(self.copies_root).as_posix(), **pin(raw)}

    def capture_copies(self, guard: dict, boot: dict) -> Path:
        self.copies_root = Path(self.registration["copies_root"])
        require(self.copies_root == self.control / "copies", "fixed new capture directory")
        self._mkdir_new(self.copies_root)
        for name in ("authentication", "artifacts", "temp"):
            self._mkdir_new(self.copies_root / name)
        self.observe_startup(4)
        members, total = [], 0
        for name in ("owner.sqlite3", "lineage.sqlite3"):
            source = self.root / "state" / name
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    (source.with_name(name + suffix)).lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise PlatformValidationRequired("source SQLite sidecar present; no copy/repair/replay")
            handle, info, _ = self._open(source, data=True, immutable=True)
            self.copy_fences.append(handle)
            # Avoid _copy retaining a second deny-write DB handle: only these
            # explicitly tracked fences are released before schema migration.
            require((info.size_high << 32) | info.size_low <= 64 * 1024 * 1024,
                    "source SQLite exceeds bounded capture before read")
            raw = source.read_bytes()
            require(len(raw) <= 64 * 1024 * 1024 and raw[:16] == b"SQLite format 3\0" and raw[18:20] == b"\x01\x01",
                    "bounded rollback-mode source SQLite required")
            self._new_bytes(self.copies_root / name, raw)
            members.append({"relative_path": name, **pin(raw)})
            total += len(raw)
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    source.with_name(name + suffix).lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise PlatformValidationRequired("SQLite sidecar appeared during held copy")
        for key, name in (("owner", "baseline-owner.sqlite3"), ("ledger", "baseline-lineage.sqlite3")):
            member = self._copy(self.depot / "reference" / name, self.copies_root / name, maximum=64 * 1024 * 1024)
            require({k: member[k] for k in ("byte_length", "sha256")} == validate_pin(self.registration["baseline_pins"][key]), "original baseline bytes differ")
            members.append(member)
            total += member["byte_length"]
        for relative, source in (("authentication", self.root / "authentication"),
                                 ("artifacts", self.root / "artifacts"), ("temp", self.root / "state" / "temp")):
            names = sorted(path.name for path in source.iterdir())
            require(len(names) + len(members) <= MAX_COPY_MEMBERS, "bounded complete copy membership")
            for name in names:
                require(name not in {".", ".."} and "/" not in name and "\\" not in name, "capture member name")
                member = self._copy(source / name, self.copies_root / relative / name)
                members.append(member)
                total += member["byte_length"]
                require(total <= MAX_COPY_BYTES, "complete copy total capacity exceeded")
            require(names == sorted(path.name for path in source.iterdir()), "source directory changed during guarded cut")
        manifest = {"contract_version": "policy-owner-admission-cut-copies-v2", "binding_identity": self.binding["identity"],
                    "boot_id": boot["new_boot_id"], "guard_identity": guard["identity"], "files": members}
        manifest_raw = canonical(manifest)
        self._new_bytes(self.copies_root / "copies-manifest.json", manifest_raw)
        self._cut_body = {"contract_version": "policy-owner-admission-maintenance-cut-v2",
            "binding_identity": self.binding["identity"], "old_boot_id": guard["old_boot_id"], "new_boot_id": boot["new_boot_id"],
            "guard_identity": guard["identity"], "guard_record_path": str(self.control / "guard.json"), "guard_record_pin": pin(canonical(guard)),
            "boot_record_path": str(self.control / "boot.json"), "boot_record_pin": pin(canonical(boot)),
            "phases": ["GUARD_ESTABLISHED", "FRESH_KERNEL_BOOT_OBSERVED", "LEGACY_STATE_CLASSIFIED"],
            "copies_manifest_pin": pin(manifest_raw)}
        self._physical_cut_ready = True
        # This is only a physical snapshot prerequisite. No persisted cut claims
        # LEGACY_STATE_CLASSIFIED until the actual classifier succeeds below.
        return self.copies_root

    def classify(self, copies: Path):
        return classify_legacy_copies(copies, self.binding, self.role_raw, self, self.registration["baseline_pins"])

    def save_classification(self, classification) -> None:
        self._new_bytes(self.control / "classification.json", canonical(classification.evidence))
        if classification.kind == "ELIGIBLE":
            require(Path(self.registration["cut_record_path"]) == self.control / "cut.json", "fixed new cut path")
            self.cut = seal("policy-owner-admission-maintenance-cut:v2", self._cut_body)
            self._new_bytes(self.control / "cut.json", canonical(self.cut))
            self._cut_ready = True

    def release_copy_fences(self) -> None:
        self.observe_startup(4)
        require(self._cut_ready and self.observe_boot() == self.cut["new_boot_id"], "continuous maintenance cut lost")
        for handle in self.copy_fences:
            self._ok(self.k.CloseHandle(handle))
            self.held.remove(handle)
            self._opened = {key: value for key, value in self._opened.items() if value[0] != handle}
        self.copy_fences.clear()
        self._owner_sql_authorized = True

    def assert_live(self, binding: dict, epoch: int | None, attempt: str | None, phase: str) -> None:
        self._budget()
        require(binding == self.binding and self._gate_handle is not None and self._lease_handle is not None,
                "same owned maintenance lease required")
        require((self.control / ("lease-" + self._lease + ".json")).read_bytes() == self._lease_bytes,
                "maintenance lease bytes changed")
        self._require_operator_admin()
        self.observe_startup(self._expected_start)
        if self._cut_ready:
            require(self.observe_boot() == self.cut["new_boot_id"], "maintenance kernel identity changed")
        for path, field in ((self.root / "state" / "owner.sqlite3", "owner_state_object_id"),
                            (self.root / "state" / "lineage.sqlite3", "ledger_object_id")):
            _, info, _ = self._open(path)
            require(f"{info.volume:08x}:{info.index_high:08x}{info.index_low:08x}" == self.legacy[field], "maintenance durable object replaced")

    def _assert_startup(self) -> None:
        self.observe_startup(self._expected_start)

    def assert_cut(self, binding: dict) -> dict:
        require(self._cut_ready and self.cut is not None, "actual post-boot physical cut not established")
        self.assert_live(binding, None, None, "MAINTENANCE_CUT")
        require(self.cut["old_boot_id"] != self.cut["new_boot_id"], "fresh kernel cut required")
        self._validate_cut_records()
        return {**self.cut, "lease_owner_id": self._lease, "context": self.context}

    def assert_classification_cut(self, binding: dict) -> dict:
        require(self._physical_cut_ready and binding == self.binding, "complete physical post-boot snapshot required")
        self.assert_live(binding, None, None, "CLASSIFICATION_CUT")
        require(self.observe_boot() == self._cut_body["new_boot_id"] != self._cut_body["old_boot_id"], "fresh kernel snapshot continuity")
        return {"guard_identity": self._cut_body["guard_identity"], "new_boot_id": self._cut_body["new_boot_id"],
                "copies_manifest_pin": self._cut_body["copies_manifest_pin"], "context": self.context,
                "classification_completed": False}

    def assert_copies(self, root: Path, binding: dict) -> None:
        self.assert_classification_cut(binding)
        require(root == self.copies_root, "exact owned maintenance copies")
        manifest = self._pinned_json(str(root / "copies-manifest.json"), self._cut_body["copies_manifest_pin"])
        closed(manifest, "contract_version binding_identity boot_id guard_identity files", "complete cut-copy manifest")
        require(manifest["binding_identity"] == self.binding["identity"] and manifest["boot_id"] == self._cut_body["new_boot_id"] and
                manifest["guard_identity"] == self._cut_body["guard_identity"], "physical copy bindings")
        expected = set()
        for member in manifest["files"]:
            closed(member, "relative_path byte_length sha256", "copy member")
            relative = member["relative_path"]
            require(type(relative) is str and relative not in expected and not any(part in {"", ".", ".."} for part in relative.split("/")) and
                    ":" not in relative and "\\" not in relative, "closed copy member path")
            expected.add(relative)
            path = root / relative
            self._protect_path(path, immutable=True)
            require(pin(path.read_bytes()) == {k: member[k] for k in ("byte_length", "sha256")}, "captured member bytes differ")
        require({p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"copies-manifest.json"} == expected,
                "complete copy file inventory changed")

    def assert_closed_handoff(self, binding: dict, previous_lease_id: str) -> None:
        raise PlatformValidationRequired("maintenance never adopts an existing live ceremony")

    def verify_frontend(self, frontend: dict, binding: dict) -> None:
        raise PlatformValidationRequired("maintenance cannot admit a frontend or authentication")

    def verify_creation_parameters(self, frontend: dict, challenge: dict, receipt: str) -> None:
        raise PlatformValidationRequired("maintenance cannot authenticate")

    def publish_immutable(self, path: Path, raw: bytes) -> None:
        raise PlatformValidationRequired("maintenance cannot publish authentication/artifact effects")

    def store(self) -> EnrollmentOnlyStore:
        return EnrollmentOnlyStore(StorePaths(self.root), self.binding, self.role_raw, self)

    def stage_registration(self) -> None:
        import winreg
        self.assert_cut(self.binding)
        value = {**self.registration, "cut_record_pin": pin(canonical(self.cut))}
        key_path = "SOFTWARE\\TradingAgents\\PolicyOwner\\" + self.legacy["instance"]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            self._descriptor(int(key), registry=True)
            try:
                winreg.QueryValueEx(key, "AdmissionV2Json")
            except FileNotFoundError:
                pass
            else:
                raise PlatformValidationRequired("existing v2 registration is not overwritten or resumed")
            self.journal("REGISTRATION_STAGE_INTENT", {"registration_pin": pin(canonical(value))})
            self._write_key_handle = int(key)
            try:
                winreg.SetValueEx(key, "AdmissionV2Json", 0, winreg.REG_SZ, canonical(value).decode("utf-8"))
                winreg.FlushKey(key)
            finally:
                self._write_key_handle = None
        self.registration = value
        self._staged = True
        self.verify_registration()

    def verify_registration(self) -> None:
        require(self._staged and self._registry_json("AdmissionV2Json") == self.registration,
                "new protected v2 registration readback differs")

    def finalize_activation(self, observed: dict) -> dict:
        import winreg
        self.verify_registration()
        require(self.registration["activation_record_pin"] is None and observed == self.observe_startup(3),
                "activation requires measured demand startup and unactivated new registration")
        state = self.store().inspect()
        require(state["migration_phase"] == "V2_ACTIVATED_CLOSED" and state["phase"] == "CLOSED" and state["attempt"] is None,
                "activation must leave an unused closed gate")
        activation = seal("policy-owner-admission-activation:v2", {"contract_version": "policy-owner-admission-activation-v2",
            "binding_identity": self.binding["identity"], "cut_identity": self.cut["identity"],
            "migration_phase": "V2_ACTIVATED_CLOSED", "gate": "CLOSED", "attempt": None,
            "startup": observed, "recorded_at_utc": datetime.now(timezone.utc).isoformat()})
        self._new_bytes(self.control / "activation.json", canonical(activation))
        previous = canonical(self.registration)
        updated = {**self.registration, "activation_record_pin": pin(canonical(activation))}
        key_path = "SOFTWARE\\TradingAgents\\PolicyOwner\\" + self.legacy["instance"]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            self._descriptor(int(key), registry=True)
            current, kind = winreg.QueryValueEx(key, "AdmissionV2Json")
            require(kind == winreg.REG_SZ and current.encode("utf-8") == previous, "new registration changed before activation pin update")
            self._write_key_handle = int(key)
            try:
                winreg.SetValueEx(key, "AdmissionV2Json", 0, winreg.REG_SZ, canonical(updated).decode("utf-8"))
                winreg.FlushKey(key)
            finally:
                self._write_key_handle = None
        self.registration = updated
        self.verify_registration()
        return activation

    def close(self) -> None:
        failures = []
        for handle in reversed(getattr(self, "service_handles", [])):
            if not self.a.CloseServiceHandle(handle):
                failures.append({"kind": "service", "error": c.get_last_error()})
        self.service_handles = []
        for handle in reversed(self.held):
            if not self.k.CloseHandle(handle):
                failures.append({"kind": "file_or_process", "error": c.get_last_error()})
        self.held.clear()
        self._opened.clear()
        self._gate_handle = self._lease_handle = None
        require(not failures, "maintenance own-handle release UNKNOWN: " + str(failures))
        self._budget()


def run_authorized_maintenance(instance: str, step: str, confirmation: str, depot: Path) -> dict:
    backend = WindowsMaintenanceAuthority(instance, step, confirmation, depot)
    return MaintenanceCoordinator(backend).run(step)

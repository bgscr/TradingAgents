"""Formal Windows authority for enrollment v2; imported only after v2 framing.

Importing this module performs no Windows calls. Production construction has no
synthetic fallback. The pinned native parent owns the physical lease for the
whole ceremony; this worker independently verifies the actual OS objects.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import re
import sys

from tradingagents.policy_owner_admission_v2 import (
    PlatformAuthority, PlatformValidationRequired, canonical, closed, frontend_value,
    identity, pin, require, strict_json, validate_binding, validate_pin,
)


class _FileInfo(ctypes.Structure):
    _fields_ = [(name, w.DWORD) for name in (
        "attributes", "creation_low", "creation_high", "access_low", "access_high",
        "write_low", "write_high", "volume", "size_high", "size_low", "links", "index_high", "index_low")]


class WindowsAuthority(PlatformAuthority):
    """Concrete OS capability. Every request reacquires and validates its evidence."""
    context = "PRODUCTION"

    def __init__(self, frame: dict, depot: Path) -> None:
        require(os.name == "nt" and ctypes.sizeof(ctypes.c_void_p) == 8, "Windows x64 authority required")
        require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode, "isolated worker required")
        self.binding = validate_binding(frame["anchor_v2"])
        self.legacy = self.binding["legacy_setup_binding"]
        self._lease = frame["lease_id"]
        require(type(self._lease) is str and re.fullmatch("[0-9a-f]{64}", self._lease), "physical lease identifier")
        self.root = Path(self.legacy["owner_root"])
        self.depot = depot.absolute()
        require(self.depot == self.root / "admission-v2", "same-instance v2 depot")
        self.control = self.root / "state" / "admission-v2"
        self.held: list[int] = []
        self._opened: dict[tuple, tuple] = {}
        self._bound_frontend: dict | None = None
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.a = ctypes.WinDLL("advapi32", use_last_error=True)
        self._declare()
        try:
            require(self._token_sid(self.k.GetCurrentProcess()) == self.legacy["service_sid"], "dedicated service token required")
            require(self.k.GetFileType(self.k.GetStdHandle(-10)) == 3, "native-owned anonymous pipe required")
            self.registration = self._registration()
            require(self.registration["binding"] == self.binding and Path(self.registration["depot_path"]) == self.depot,
                    "protected registration differs from request/code depot")
            self._hold_original_objects()
            self._hold_depot()
            self.profile = self._pinned_json(self.registration["platform_profile_path"], self.registration["platform_profile_pin"])
            self._validate_platform_report()
            self.cut = self._pinned_json(self.registration["cut_record_path"], self.registration["cut_record_pin"])
            self._lease_record = self._protected_json(self.control / ("lease-" + self._lease + ".json"))
            closed(self._lease_record, "contract_version lease_id binding_identity holder holder_handles", "physical lease")
            require(self._lease_record["contract_version"] == "policy-owner-admission-lease-v2" and
                    self._lease_record["lease_id"] == self._lease and self._lease_record["binding_identity"] == self.binding["identity"],
                    "physical lease identity mismatch")
            self._duplicate_owned_lease()
            self.assert_live(self.binding, None, None, "BOOTSTRAP")
            self.assert_cut(self.binding)
            self._validate_activation()
        except BaseException as primary:
            try:
                self.close()
            except BaseException as release:
                raise BaseExceptionGroup("Windows authority construction and release both failed", [primary, release])
            raise

    def _declare(self) -> None:
        k, a = self.k, self.a
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.GetCurrentProcess.restype = w.HANDLE
        k.GetStdHandle.argtypes = [w.DWORD]; k.GetStdHandle.restype = w.HANDLE
        k.GetFileType.argtypes = [w.HANDLE]
        k.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.POINTER(_FileInfo)]
        k.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(ctypes.c_uint64)] * 4)]
        k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k.OpenProcess.restype = w.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
        k.MoveFileExW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
        a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        a.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
        k.LocalFree.argtypes = [ctypes.c_void_p]; k.LocalFree.restype = ctypes.c_void_p
        a.GetKernelObjectSecurity.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        a.RegGetKeySecurity.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, ctypes.POINTER(w.DWORD)]
        a.GetSecurityDescriptorOwner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)]
        a.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.BOOL)]
        a.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        a.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD, *([ctypes.POINTER(ctypes.c_void_p)] * 5)]
        a.GetSecurityDescriptorLength.argtypes = [ctypes.c_void_p]

    def _ok(self, result: object) -> None:
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def _sid(self, pointer: int) -> str:
        value = w.LPWSTR()
        self._ok(self.a.ConvertSidToStringSidW(pointer, ctypes.byref(value)))
        try:
            return value.value
        finally:
            self.k.LocalFree(ctypes.cast(value, ctypes.c_void_p))

    def _token_sid(self, process: int) -> str:
        token = w.HANDLE()
        self._ok(self.a.OpenProcessToken(process, 8, ctypes.byref(token)))
        try:
            size = w.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            require(0 < size.value <= 65536, "bounded actual token information")
            raw = ctypes.create_string_buffer(size.value)
            self._ok(self.a.GetTokenInformation(token, 1, raw, size, ctypes.byref(size)))
            return self._sid(ctypes.c_void_p.from_buffer(raw).value)
        finally:
            self.k.CloseHandle(token)

    def _descriptor(self, handle: int, *, registry: bool = False) -> bytes:
        size = w.DWORD()
        if registry:
            self.a.RegGetKeySecurity(handle, 5, None, ctypes.byref(size))
        else:
            owner, group, dacl, sacl, pointer = (ctypes.c_void_p() for _ in range(5))
            require(self.a.GetSecurityInfo(handle, 1, 7, ctypes.byref(owner), ctypes.byref(group), ctypes.byref(dacl), ctypes.byref(sacl), ctypes.byref(pointer)) == 0,
                    "protected file descriptor query failed")
            try:
                size.value = self.a.GetSecurityDescriptorLength(pointer)
                require(0 < size.value <= 65536, "bounded protected descriptor")
                copied = ctypes.string_at(pointer, size.value)
            finally:
                self.k.LocalFree(pointer)
        require(0 < size.value <= 65536, "bounded protected descriptor")
        raw = ctypes.create_string_buffer(size.value)
        if registry:
            require(self.a.RegGetKeySecurity(handle, 5, raw, ctypes.byref(size)) == 0, "registry descriptor query failed")
        else:
            ctypes.memmove(raw, copied, len(copied))
        owner, acl, present, defaulted = ctypes.c_void_p(), ctypes.c_void_p(), w.BOOL(), w.BOOL()
        self._ok(self.a.GetSecurityDescriptorOwner(raw, ctypes.byref(owner), ctypes.byref(defaulted)))
        owner_sid = self._sid(owner.value)
        trusted = {"S-1-5-18", "S-1-5-32-544"}
        if not registry:
            trusted.add(self.legacy["service_sid"])
        require(owner_sid in trusted, "untrusted object owner")
        self._ok(self.a.GetSecurityDescriptorDacl(raw, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)))
        require(present.value and acl.value, "null/missing protected DACL")
        ace_count = ctypes.c_ushort.from_address(acl.value + 4).value
        mutation = (0x000D0026 if registry else 0x000D0156) | 0x50000000
        suppress_owner = owner_sid != self.legacy["service_sid"]
        for index in range(ace_count):
            ace = ctypes.c_void_p(); self._ok(self.a.GetAce(acl, index, ctypes.byref(ace)))
            kind = ctypes.c_ubyte.from_address(ace.value).value
            require(kind in (0, 1), "unsupported callback/object DACL entry")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = self._sid(ace.value + 8)
            if kind == 0 and mask & mutation:
                require(sid in trusted, "unrelated principal can mutate protected object")
            if kind == 1 and sid == "S-1-3-4" and mask & 0x000C0000 == 0x000C0000:
                suppress_owner = True
        require(suppress_owner, "service implicit owner DACL rights not suppressed")
        return raw.raw[:size.value]

    def _registry_json(self, name: str) -> dict:
        import winreg
        instance = self.legacy["instance"]
        require(re.fullmatch(r"(?:operational|test)-[0-9]{8}-[0-9]{3}", instance), "instance syntax")
        result = None
        for path in (r"SOFTWARE\TradingAgents", r"SOFTWARE\TradingAgents\PolicyOwner",
                     "SOFTWARE\\TradingAgents\\PolicyOwner\\" + instance):
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                self._descriptor(int(key), registry=True)
                if path.endswith("\\" + instance):
                    raw, kind = winreg.QueryValueEx(key, name)
                    require(kind == winreg.REG_SZ and type(raw) is str, "unexpanded protected registry JSON required")
                    result = strict_json(raw.encode("utf-8"), canonical_required=True)
        require(result is not None, "missing protected registration")
        return result

    def _registration(self) -> dict:
        value = self._registry_json("AdmissionV2Json")
        closed(value, "contract_version binding depot_path cut_record_path cut_record_pin platform_profile_path platform_profile_pin copies_root baseline_pins activation_record_path activation_record_pin", "v2 registration")
        require(value["contract_version"] == "windows-policy-owner-admission-anchor-v2", "v2 registration contract")
        return value

    def _open(self, path: Path, *, data: bool = False, immutable: bool = False) -> tuple[int, _FileInfo, bytes]:
        require(path.is_absolute() and path == path.absolute() and path.is_relative_to(self.root), "protected same-instance absolute path")
        cache_key = (str(path).casefold(), data, immutable)
        if cache_key in self._opened:
            return self._opened[cache_key]
        handle = self.k.CreateFileW(str(path), 0x20080 | (0x80000000 if data else 0), 1 if immutable else 3,
                                    None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.held.append(handle)
        info = _FileInfo(); self._ok(self.k.GetFileInformationByHandle(handle, ctypes.byref(info)))
        require(not info.attributes & 0x400 and (info.attributes & 0x10 or info.links == 1), "reparse/hard-linked protected object")
        result = handle, info, self._descriptor(handle)
        self._opened[cache_key] = result
        return result

    def _protect_path(self, path: Path, *, immutable: bool) -> None:
        for parent in reversed(path.parents):
            if parent == self.root or parent.is_relative_to(self.root):
                self._open(parent)
        self._open(path, data=True, immutable=immutable)

    def _protected_json(self, path: Path) -> dict:
        self._protect_path(path, immutable=False)
        raw = path.read_bytes(); require(len(raw) <= 64 * 1024 * 1024, "protected JSON size")
        return strict_json(raw, canonical_required=True)

    def _pinned_json(self, path: str, expected: dict) -> dict:
        value = Path(path); self._protect_path(value, immutable=True)
        raw = value.read_bytes(); require(pin(raw) == validate_pin(expected), "protected pin changed")
        return strict_json(raw, canonical_required=True)

    def _hold_original_objects(self) -> None:
        self._anchor_ancestors()
        for path, field in ((self.root, "owner_root_object_id"), (self.root / "state" / "owner.sqlite3", "owner_state_object_id"),
                            (self.root / "state" / "lineage.sqlite3", "ledger_object_id")):
            _, info, descriptor = self._open(path)
            object_id = f"{info.volume:08x}:{((info.index_high << 32) | info.index_low):016x}"
            require(object_id == self.legacy[field], "original durable object identity changed")
            if path == self.root:
                require(pin(descriptor) == self.legacy["owner_root_descriptor_pin"], "original root descriptor changed")
        for path in (self.root / "state", self.root / "state" / "temp", self.root / "authentication", self.root / "artifacts", self.control):
            self._open(path)

    def _anchor_ancestors(self) -> None:
        # A maintenance process has no native parent to anchor its path. Hold
        # the complete drive-to-root chain without DELETE sharing. Ancestors
        # outside the owner root are metadata-only, not service-owned objects.
        for directory in reversed([self.root, *self.root.parents]):
            handle = self.k.CreateFileW(str(directory), 0x80, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.held.append(handle)
            info = _FileInfo(); self._ok(self.k.GetFileInformationByHandle(handle, ctypes.byref(info)))
            require(info.attributes & 0x10 and not info.attributes & 0x400, "owner ancestor type/reparse changed")

    def _hold_depot(self) -> None:
        implementation = self.binding["implementation"]
        manifest = self._pinned_json(str(self.depot / "worker-manifest.json"), implementation["worker_manifest_pin"])
        closed(manifest, "contract_version source_commit files execution_authorized", "worker manifest")
        require(manifest["contract_version"] == "policy-owner-admission-worker-depot-v2" and
                manifest["source_commit"] == implementation["source_commit"] and manifest["execution_authorized"] is False,
                "worker implementation source identity")
        observed = set()
        for item in manifest["files"]:
            closed(item, "relative_path byte_length sha256", "worker file")
            relative = item["relative_path"]
            require(type(relative) is str and (relative.startswith("source/") or relative.startswith("python/") or
                    relative in {"bin/PolicyOwner.exe", "bin/ConditionalRuntimeProtectionHost.exe"}) and
                    not any(part in ("", ".", "..") for part in relative.split("/")) and ":" not in relative and "\\" not in relative,
                    "worker manifest path")
            require(relative.casefold() not in observed, "duplicate worker path"); observed.add(relative.casefold())
            path = self.depot / relative; self._protect_path(path, immutable=True)
            require(pin(path.read_bytes()) == {"byte_length": item["byte_length"], "sha256": item["sha256"]}, "worker bytes changed")
        for subtree in ("source", "python", "bin"):
            for base, directories, files in os.walk(self.depot / subtree, followlinks=False):
                for directory in directories:
                    self._open(Path(base) / directory)
                for name in files:
                    require((Path(base) / name).relative_to(self.depot).as_posix().casefold() in observed, "unlisted worker/runtime file")
        require(Path(sys.executable).absolute() == self.depot / "python" / "python.exe", "wrong worker interpreter")
        path = self.depot / "bin" / "PolicyOwner.exe"; self._protect_path(path, immutable=True)
        require(pin(path.read_bytes()) == implementation["native_pin"], "native implementation pin changed")

    def _process(self, pid: int, expected_sid: str) -> dict:
        handle = self.k.OpenProcess(0x101000, False, pid)
        self._ok(handle); self.held.append(handle)
        require(self.k.WaitForSingleObject(handle, 0) == 258, "bound process is not alive")
        require(self._token_sid(handle) == expected_sid, "actual process token mismatch")
        name = ctypes.create_unicode_buffer(32768); length = w.DWORD(len(name))
        self._ok(self.k.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)))
        require(Path(name.value) == self.depot / "bin" / "PolicyOwner.exe", "actual process image path mismatch")
        creation, exit_time, kernel, user = (ctypes.c_uint64() for _ in range(4))
        self._ok(self.k.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)))
        return {"pid": pid, "creation_filetime": creation.value, "image_pin": self.binding["implementation"]["native_pin"]}

    def _parent(self) -> int:
        require(self.profile["parent_information_class"] == 0 and self.profile["parent_information_size"] == 48,
                "owned-parent query layout is not qualified")
        api = ctypes.WinDLL("ntdll", use_last_error=True)
        query = api.NtQueryInformationProcess
        query.argtypes = [w.HANDLE, w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]
        query.restype = w.LONG
        raw = ctypes.create_string_buffer(48); returned = w.ULONG()
        require(query(self.k.GetCurrentProcess(), 0, raw, 48, ctypes.byref(returned)) == 0 and returned.value == 48,
                "own worker parent query failed/layout changed")
        require(int.from_bytes(raw.raw[32:40], "little") == os.getpid(), "own worker query identity mismatch")
        parent = int.from_bytes(raw.raw[40:48], "little")
        require(0 < parent <= 0xFFFFFFFF, "owned worker parent is unavailable")
        return parent

    @property
    def lease_owner_id(self) -> str:
        return self._lease

    def _duplicate_owned_lease(self) -> None:
        holder = self._lease_record["holder"]
        require(self._parent() == holder["pid"] and self._process(holder["pid"], self.legacy["service_sid"]) == holder,
                "cannot duplicate from a different native parent generation")
        parent = self.k.OpenProcess(0x101040, False, holder["pid"])
        self._ok(parent); self.held.append(parent)
        require(self.k.WaitForSingleObject(parent, 0) == 258 and self._token_sid(parent) == self.legacy["service_sid"], "owned native parent unavailable")
        creation, exit_time, kernel, user = (ctypes.c_uint64() for _ in range(4))
        self._ok(self.k.GetProcessTimes(parent, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)))
        require(creation.value == holder["creation_filetime"], "parent PID reused before handle duplication")
        handles = closed(self._lease_record["holder_handles"], "gate lease", "owned physical handles")
        for kind, path in (("gate", self.control / "owner-gate.lock"), ("lease", self.control / ("lease-" + self._lease + ".json"))):
            require(type(handles[kind]) is int and 0 < handles[kind] < 2**63, "native handle value")
            duplicate = w.HANDLE()
            self._ok(self.k.DuplicateHandle(parent, handles[kind], self.k.GetCurrentProcess(), ctypes.byref(duplicate), 0, False, 2))
            self.held.append(duplicate.value)
            actual = _FileInfo(); self._ok(self.k.GetFileInformationByHandle(duplicate, ctypes.byref(actual)))
            _, expected, _ = self._open(path)
            require((actual.volume, actual.index_high, actual.index_low) == (expected.volume, expected.index_high, expected.index_low),
                    "duplicated handle is not the exact owned gate/lease object")
            self._descriptor(duplicate.value)
        # Duplicates refer to the same file objects, retaining ShareRead-only
        # protection until this worker exits, even if its native parent dies.

    def assert_live(self, binding: dict, epoch: int | None, attempt: str | None, phase: str) -> None:
        require(binding == self.binding and self._registration() == self.registration, "live protected binding changed")
        holder = self._lease_record["holder"]
        require(self._parent() == holder["pid"] and self._process(holder["pid"], self.legacy["service_sid"]) == holder,
                "worker lost the exact native parent generation")
        if self._bound_frontend is not None:
            require(self._process(self._bound_frontend["pid"], self.legacy["operator_sid"]) == self._bound_frontend,
                    "bound frontend exited or changed before a durable effect")
        for path in (self.control / "owner-gate.lock", self.control / ("lease-" + self._lease + ".json")):
            probe = self.k.CreateFileW(str(path), 0x40000000, 3, None, 3, 0x02200000, None)
            if probe != ctypes.c_void_p(-1).value:
                self.k.CloseHandle(probe)
                raise PlatformValidationRequired("exclusive native write-sharing barrier absent")
            require(ctypes.get_last_error() == 32, "lease barrier failure is not sharing denial")

    def _boot_identity(self) -> str:
        import uuid
        profile = self.profile
        closed(profile, "contract_version os_version architecture boot_information_class boot_information_size parent_information_class parent_information_size validation_report_path validation_report_pin identity", "platform profile")
        require(profile["identity"] == identity("policy-owner-admission-platform-profile:v2", {k: v for k, v in profile.items() if k != "identity"}), "profile identity")
        version = sys.getwindowsversion().platform_version
        # The native side pins the .NET version string too; a profile must be
        # issued for this exact OS build after the separate platform campaign.
        require(profile["contract_version"] == "policy-owner-admission-platform-profile-v2" and profile["architecture"] == "x64" and
                profile["os_version"] == ".".join(str(part) for part in version) and
                profile["boot_information_class"] == 90 and profile["boot_information_size"] == 32, "unqualified OS boot adapter")
        api = ctypes.WinDLL("ntdll", use_last_error=True)
        query = api.NtQuerySystemInformation
        query.argtypes = [w.ULONG, ctypes.c_void_p, w.ULONG, ctypes.POINTER(w.ULONG)]; query.restype = w.LONG
        raw = ctypes.create_string_buffer(32); returned = w.ULONG()
        require(query(90, raw, 32, ctypes.byref(returned)) == 0 and returned.value == 32, "boot identity query failed/layout changed")
        flags = int.from_bytes(raw.raw[24:32], "little"); require(flags & 6 == 0, "hibernation/soft boot not admitted")
        value = uuid.UUID(bytes_le=raw.raw[:16]); require(value.int != 0, "empty boot identity")
        return str(value)

    def _assert_startup(self) -> None:
        import winreg
        instance = self.legacy["instance"]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Services\\TA.PolicyOwner." + instance,
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            self._descriptor(int(key), registry=True)
            def get(name: str, default: object = None) -> object:
                try: return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError: return default
            require(get("ImagePath") == '"' + str(self.depot / "bin" / "PolicyOwner.exe") + '" service-v2 ' + instance and
                    get("Start") == 3 and get("Type") == 16 and get("ObjectName") == "NT SERVICE\\TA.PolicyOwner." + instance,
                    "startup guard no longer selects only the new owner")
            require(get("FailureCommand", "") == "" and get("DelayedAutoStart", 0) == 0, "uncontrolled recovery/startup")
            try:
                with winreg.OpenKey(key, "TriggerInfo"):
                    raise PlatformValidationRequired("unqualified service triggers")
            except FileNotFoundError:
                pass

    def assert_cut(self, binding: dict) -> dict:
        require(binding == self.binding, "cut binding mismatch")
        cut = self.cut
        closed(cut, "contract_version binding_identity old_boot_id new_boot_id guard_identity guard_record_path guard_record_pin boot_record_path boot_record_pin phases copies_manifest_pin identity", "maintenance cut")
        require(cut["identity"] == identity("policy-owner-admission-maintenance-cut:v2", {k: v for k, v in cut.items() if k != "identity"}), "cut identity")
        require(cut["contract_version"] == "policy-owner-admission-maintenance-cut-v2" and cut["binding_identity"] == binding["identity"] and
                cut["old_boot_id"] != cut["new_boot_id"] and cut["new_boot_id"] == self._boot_identity() and
                cut["phases"] == ["GUARD_ESTABLISHED", "FRESH_KERNEL_BOOT_OBSERVED", "LEGACY_STATE_CLASSIFIED"], "fresh kernel/guard/copy protocol incomplete")
        self._validate_cut_records()
        self._assert_startup()
        return {**cut, "lease_owner_id": self._lease, "context": self.context}

    def _validate_platform_report(self) -> None:
        report = self._pinned_json(self.profile["validation_report_path"], self.profile["validation_report_pin"])
        closed(report, "contract_version source_commit native_pin os_version result controls evidence execution_authorized identity", "platform validation report")
        required = {"full-kernel-boot-vs-resume", "disabled-startup-across-boot", "service-sid-and-token", "registry-and-file-acl",
                    "lease-duplicate-handle-lifetime", "owned-worker-parent-query", "webauthn-user-handle", "sqlite-durability-and-cut-copies"}
        require(report["identity"] == identity("policy-owner-admission-platform-validation:v2", {k: v for k, v in report.items() if k != "identity"}) and
                report["contract_version"] == "policy-owner-admission-platform-validation-v2" and report["result"] == "PLATFORM_VALIDATION_PASSED" and
                report["execution_authorized"] is False and report["source_commit"] == self.binding["implementation"]["source_commit"] and
                report["native_pin"] == self.binding["implementation"]["native_pin"] and report["os_version"] == self.profile["os_version"] and
                type(report["controls"]) is list and set(report["controls"]) == required and len(report["controls"]) == len(required), "platform report coverage/binding invalid")
        require(type(report["evidence"]) is list and len(report["evidence"]) >= len(required), "raw platform validation evidence absent")
        for reference in report["evidence"]:
            closed(reference, "path pin", "platform evidence")
            path = Path(reference["path"]); self._protect_path(path, immutable=True)
            require(pin(path.read_bytes()) == validate_pin(reference["pin"]), "raw platform evidence changed")

    def _validate_cut_records(self) -> None:
        guard = self._pinned_json(self.cut["guard_record_path"], self.cut["guard_record_pin"])
        boot = self._pinned_json(self.cut["boot_record_path"], self.cut["boot_record_pin"])
        closed(guard, "contract_version binding_identity legacy_binding_identity profile_identity old_boot_id disabled_startup recorded_at_utc identity", "startup guard receipt")
        closed(boot, "contract_version binding_identity guard_identity new_boot_id disabled_startup recorded_at_utc identity", "fresh boot receipt")
        require(guard["identity"] == identity("policy-owner-admission-startup-guard:v2", {k: v for k, v in guard.items() if k != "identity"}) and
                boot["identity"] == identity("policy-owner-admission-fresh-boot:v2", {k: v for k, v in boot.items() if k != "identity"}) and
                guard["contract_version"] == "policy-owner-admission-startup-guard-v2" and boot["contract_version"] == "policy-owner-admission-fresh-boot-v2" and
                guard["identity"] == boot["guard_identity"] == self.cut["guard_identity"] and
                guard["binding_identity"] == boot["binding_identity"] == self.binding["identity"] and
                guard["legacy_binding_identity"] == self.legacy["identity"] and guard["profile_identity"] == self.profile["identity"] and
                guard["old_boot_id"] == self.cut["old_boot_id"] and boot["new_boot_id"] == self.cut["new_boot_id"], "raw guard/boot chronology binding mismatch")
        expected = {"image_path": '"' + str(self.depot / "bin" / "PolicyOwner.exe") + '" service-v2 ' + self.legacy["instance"],
                    "start": 4, "type": 16, "account": "NT SERVICE\\TA.PolicyOwner." + self.legacy["instance"],
                    "failure_command": "", "delayed_auto_start": 0, "trigger_info": None}
        require(guard["disabled_startup"] == boot["disabled_startup"] == expected, "cut startup guard receipt mismatch")

    def _validate_activation(self) -> None:
        value = self._pinned_json(self.registration["activation_record_path"], self.registration["activation_record_pin"])
        closed(value, "contract_version binding_identity cut_identity migration_phase gate attempt startup recorded_at_utc identity", "activation receipt")
        expected = {"image_path": '"' + str(self.depot / "bin" / "PolicyOwner.exe") + '" service-v2 ' + self.legacy["instance"],
                    "start": 3, "type": 16, "account": "NT SERVICE\\TA.PolicyOwner." + self.legacy["instance"],
                    "failure_command": "", "delayed_auto_start": 0, "trigger_info": None}
        require(value["identity"] == identity("policy-owner-admission-activation:v2", {k: v for k, v in value.items() if k != "identity"}) and
                value["contract_version"] == "policy-owner-admission-activation-v2" and value["binding_identity"] == self.binding["identity"] and
                value["cut_identity"] == self.cut["identity"] and value["migration_phase"] == "V2_ACTIVATED_CLOSED" and value["gate"] == "CLOSED" and
                value["attempt"] is None and value["startup"] == expected, "final activation is absent/inconsistent; staged registration cannot accept requests")

    def assert_copies(self, root: Path, binding: dict) -> None:
        self.assert_cut(binding)
        require(root == Path(self.registration["copies_root"]) and root.is_relative_to(self.control), "registered maintenance copy root")
        manifest = self._pinned_json(str(root / "copies-manifest.json"), self.cut["copies_manifest_pin"])
        closed(manifest, "contract_version binding_identity boot_id guard_identity files", "copy manifest")
        require(manifest["contract_version"] == "policy-owner-admission-cut-copies-v2" and manifest["binding_identity"] == binding["identity"] and
                manifest["boot_id"] == self.cut["new_boot_id"] and manifest["guard_identity"] == self.cut["guard_identity"], "copy cut provenance")
        expected = set()
        for item in manifest["files"]:
            closed(item, "relative_path byte_length sha256", "copied file")
            relative = item["relative_path"]
            require(type(relative) is str and not any(p in ("", ".", "..") for p in relative.split("/")) and
                    ":" not in relative and "\\" not in relative and relative not in expected, "copy manifest path/duplicate")
            expected.add(relative); path = root / relative; self._protect_path(path, immutable=True)
            require(pin(path.read_bytes()) == {"byte_length": item["byte_length"], "sha256": item["sha256"]}, "copy bytes changed")
        actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} - {"copies-manifest.json"}
        require(actual == expected, "copy inventory incomplete")

    def verify_frontend(self, frontend: dict, binding: dict) -> None:
        frontend_value(frontend, binding)
        require(self._process(frontend["pid"], self.legacy["operator_sid"]) == frontend, "frontend generation changed")
        require(self._bound_frontend is None or self._bound_frontend == frontend, "worker frontend binding cannot change")
        self._bound_frontend = dict(frontend)

    def assert_closed_handoff(self, binding: dict, previous_lease_id: str) -> None:
        self.assert_live(binding, None, None, "CLOSED_HANDOFF")
        self.assert_cut(binding)
        require(re.fullmatch("[0-9a-f]{64}", previous_lease_id) is not None, "previous closed lease identity")
        if previous_lease_id == self._lease:
            return
        path = self.control / ("lease-" + previous_lease_id + ".json")
        prior = self._protected_json(path)
        require(prior["lease_id"] == previous_lease_id and prior["binding_identity"] == binding["identity"], "closed handoff evidence mismatch")
        probe = self.k.CreateFileW(str(path), 0x40000000, 3, None, 3, 0x02200000, None)
        require(probe != ctypes.c_void_p(-1).value, "previous owner still holds its lease or absence is unknown")
        self.k.CloseHandle(probe)  # no write, no delete, no old process access

    def verify_creation_parameters(self, frontend: dict, challenge: dict, receipt: str) -> None:
        import base64
        digest = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        value = self._protected_json(self.control / ("creation-" + digest + ".json"))
        client = ('{"type":"webauthn.create","challenge":"' + challenge["challenge"].rstrip("=").replace("+", "-").replace("/", "_") +
                  '","origin":"https://' + challenge["rp_id"] + '","crossOrigin":false}').encode("utf-8")
        parameters = {"rp": challenge["rp_id"], "user_id": challenge["user_handle"], "user_size": 32, "name": self.legacy["operator_sid"],
                      "client_data": base64.b64encode(client).decode("ascii"), "attachment": 1, "verification": 1, "attestation": 1,
                      "algorithm": -7, "window_nonzero": True, "boundary": "MARSHALLED_BEFORE_CALL"}
        require(value == {"contract_version": "policy-owner-admission-creation-parameters-v2", "lease_id": self._lease,
                          "binding_identity": self.binding["identity"], "creation_receipt": receipt, "frontend": frontend,
                          "challenge": challenge, "boundary_parameters": parameters}, "trusted creation parameter receipt differs")
        self.verify_frontend(frontend, self.binding)
        # The fixed frontend + same actual process + durable service receipt
        # bind the API call. A NONE attestation alone cannot prove user.id.

    def publish_immutable(self, path: Path, raw: bytes) -> None:
        import secrets
        allowed = path == self.root / "authentication" / "00000000.json" or (
            path.parent == self.root / "artifacts" and re.fullmatch("[0-9a-f]{64}\\.blob", path.name))
        require(allowed, "enrollment-only publication target")
        temporary = self.root / "state" / "temp" / ("admission-v2-" + secrets.token_hex(32) + ".tmp")
        with temporary.open("xb", buffering=0) as stream:
            require(stream.write(raw) == len(raw), "short publication write")
            stream.flush(); os.fsync(stream.fileno())
        # MOVEFILE_WRITE_THROUGH, without REPLACE_EXISTING. On any failure the
        # temp and PUBLICATION_INTENT remain; no repair/cleanup/retry is made.
        self._ok(self.k.MoveFileExW(str(temporary), str(path), 8))

    def close(self) -> None:
        failures = []
        for handle in reversed(self.held):
            if not self.k.CloseHandle(handle):
                failures.append({"operation": "CloseHandle", "winerror": ctypes.get_last_error(), "scope": "THIS_WORKER_OWNED_HANDLE"})
        self.held.clear()
        self._opened.clear()
        if failures:
            raise PlatformValidationRequired("owned authority handle release unconfirmed: " + canonical(failures).decode("utf-8"))


def authority_from_os(frame: dict, depot: Path) -> WindowsAuthority:
    return WindowsAuthority(frame, depot)

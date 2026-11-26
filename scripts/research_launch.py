"""Attest the local supervisor at the read end of its inherited one-shot pipe."""
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import sys
import time

MODE = "portfolio-research-v3"
STAGE = "verified-supervisor-launch-v1"

def require(condition):
    if not condition:
        raise ValueError("research_launch_binding")

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False).encode()

def release_id(root, manifest):
    data=(root/"config/bounded_mainland_release.json").read_bytes()
    require(json.loads(data)==manifest and Path(manifest["deployment_root"])==root)
    return hashlib.sha256(data).hexdigest()

def envelope(root, manifest, argv, run, deadline):
    import re
    require(re.fullmatch("[a-f0-9]{32}",run) and argv.count("--decision-mode")==1
            and argv[argv.index("--decision-mode")+1]==MODE and "--auto-latest" in argv
            and "--full" in argv and "--run" in argv and "--use-project-env" in argv
            and "--check-only" not in argv and time.monotonic()<deadline<=time.monotonic()+1200)
    return dict(contract=STAGE,run=run,release=release_id(root,manifest),argv_sha256=hashlib.sha256(canonical(argv)).hexdigest(),
                mode=MODE,deadline=deadline,issuer=os.getpid())

def _process(pid):
    from ctypes import byref,sizeof
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    nt=ctypes.WinDLL("ntdll").NtQueryInformationProcess
    nt.argtypes=[w.HANDLE,w.ULONG,ctypes.c_void_p,w.ULONG,ctypes.POINTER(w.ULONG)]
    h=kernel.OpenProcess(0x1000,False,pid)
    require(h)
    try:
        name=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(name))
        require(kernel.QueryFullProcessImageNameW(h,0,name,byref(size)))
        basic=(ctypes.c_void_p*6)();length=w.ULONG()
        require(nt(h,0,basic,sizeof(basic),byref(length))==0)
        # ProcessCommandLineInformation returns a counted UNICODE_STRING.
        nt(h,60,None,0,byref(length));require(0<length.value<=131072)
        buf=ctypes.create_string_buffer(length.value)
        require(nt(h,60,buf,len(buf),byref(length))==0)
        class Unicode(ctypes.Structure):
            _fields_=[("size",w.USHORT),("maximum",w.USHORT),("address",ctypes.c_void_p)]
        u=Unicode.from_buffer(buf);low=ctypes.addressof(buf)
        require(u.size%2==0 and low<=u.address<=low+len(buf)-u.size)
        return Path(name.value),int(basic[5]),ctypes.wstring_at(u.address,u.size//2)
    finally:kernel.CloseHandle(h)

def _split(cmd):
    lib=ctypes.WinDLL("shell32").CommandLineToArgvW
    lib.argtypes=[w.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
    lib.restype=ctypes.POINTER(w.LPWSTR)
    n=ctypes.c_int();p=lib(cmd,ctypes.byref(n));require(p)
    try:return list(p[:n.value])
    finally:
        free=ctypes.WinDLL("kernel32").LocalFree;free.argtypes=[ctypes.c_void_p];free(p)

def receive(handle, root, manifest, argv, run, deadline):
    from multiprocessing.connection import PipeConnection
    require(type(handle) is int and handle>0)
    os.set_handle_inheritable(handle,False)
    with PipeConnection(handle,readable=True,writable=False) as pipe:
        peer=w.DWORD()
        get=ctypes.WinDLL("kernel32").GetNamedPipeClientProcessId
        get.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
        require(get(handle,ctypes.byref(peer)))
        issuer=peer.value
        launcher=root/".venv/Scripts/python.exe"
        parent=os.getppid()
        if parent!=issuer:
            executable,grandparent,_=_process(parent)
            require(executable==launcher and grandparent==issuer)
        executable,_,command=_process(issuer)
        args=_split(command)
        require(executable==Path(sys.base_prefix)/"python.exe" and Path(args[0]) in {executable,launcher}
                and args[1:3]==["-I","-B"] and Path(args[3])==root/"scripts/analyze_mainland_bounded.py"
                and args[4:]==argv)
        job=w.BOOL()
        test=ctypes.WinDLL("kernel32").IsProcessInJob
        test.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]
        require(test(w.HANDLE(-1),None,ctypes.byref(job)) and job.value)
        require(pipe.poll(min(3,max(0,deadline-time.monotonic()))))
        raw=pipe.recv_bytes(2048)
        expect=envelope(root,manifest,argv,run,deadline);expect["issuer"]=issuer
        require(raw==canonical(expect))
        return expect

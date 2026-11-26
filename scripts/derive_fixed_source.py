"""Offline derivation of the two reviewed fixed-source files and candidate manifest."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

REV = '9d340d35382d12c85931f60f64c3a6ad81aa3194'
BUILDER = '8a1bd6a112a34f7d61104781c3649e6f4ea380993cf12897fca35522107a57f6'
RECIPE = 'dd54604d740d31dfd5b955c0f7c549dd56daa6891098c8088fc3555e91c914e3'
PATCH = 'b7a57930ff2551af88fe2af5e64539c2d172c94667c8990f15b2dee97cd1bd0a'
FILES = ('scripts/project_credentials.py', 'scripts/diagnostic_runtime.py')
FINANCE_INPUTS = {'tests/test_financial_acceptance_scope.py':'98790d46a7ae7e9a5e477a69a04a67eea1a5ef0d42a82ca9a922ec7dce56e3f2',
                  'tests/test_current_tushare_statements.py':'bb4ef79ae84fcf17fd497c75936d4cd212a44dce5c0d963776759ce7ee0913ae'}
MANIFEST = 'config/bounded_mainland_release.json'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def require(condition, reason):
    if not condition:
        raise ValueError(reason)

def blob(repo, path, pin):
    raw = subprocess.check_output(['git','--no-replace-objects','-C',str(repo),'cat-file','blob',REV+':'+path])
    require(digest(raw)==pin, 'unreviewed source')
    return raw

def directory_id(path):
    info = path.stat()
    require(info.st_ino and path.is_dir() and path.is_absolute()
            and len(path.drive)==2 and path.drive.endswith(':')
            and all(not part.is_symlink() and not part.is_junction() for part in (path,*path.parents)), 'directory identity')
    return info.st_dev,info.st_ino

def patch_bytes():
    text=(Path(__file__).resolve().parents[1]/'config/fixed-credential-source.patch').read_bytes().replace(b'\r\n',b'\n')
    # The pinned Git blobs use CRLF; diff headers use LF. Restore that reviewed wire format.
    patch=b''.join(line.replace(b'\n',b'\r\n') if line[:1] in (b' ',b'+',b'-') and not line.startswith((b'+++ ',b'--- ')) else line for line in text.splitlines(keepends=True))
    require(digest(patch)==PATCH,'fixed patch changed')
    return patch

def derive(repo, target, reviewed_declaration, scratch):
    """Derive only; the caller independently pins the authorized machine declaration."""
    require(os.name=='nt' and sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
            and not sys.flags.optimize,'isolated Windows required')
    require(repo.parent==target.parent and repo!=target,'sibling roots required')
    declaration=(socket.gethostname().casefold(),str(target),directory_id(target),str(repo),directory_id(repo))
    require(digest(repr(declaration).encode('ascii'))==reviewed_declaration,'machine declaration mismatch')
    builder_path='scripts/build_ordinary_full.py'
    builder={'__name__':'checked_builder','__file__':str(repo/builder_path)}
    exec(compile(blob(repo,builder_path,BUILDER),builder_path,'exec'),builder)
    recipe=json.loads(blob(repo,'config/ordinary-full-release-lock.json',RECIPE))
    sources=builder['read_sources'](repo,REV,recipe)
    builder['verify_runtime'](target,recipe)
    patch=patch_bytes()
    with tempfile.TemporaryDirectory(dir=scratch) as temp:
        work=Path(temp)
        (work/'scripts').mkdir()
        for name in FILES:
            (work/name).write_bytes(sources[name])
        subprocess.run(['git','apply','--binary','--whitespace=nowarn','-'],input=patch,cwd=work,check=True)
        changed={name:(work/name).read_bytes() for name in FILES}
    marker=b'_FIXED_RELEASE = None'
    require(changed[FILES[0]].count(marker)==1,'binding template changed')
    changed[FILES[0]]=changed[FILES[0]].replace(marker,b'_FIXED_RELEASE = '+repr(declaration).encode('ascii'))
    for name,pin in FINANCE_INPUTS.items():
        changed[name]=blob(repo,name,pin)
    def manifest(changes):
        rows=[dict(root=scope,relative=name,sha256=digest(changes[name]) if scope=='project' and name in changes else pin)
              for scope,entries in {'project':recipe['project'],**recipe['runtime']}.items() for name,pin in entries.items()]
        rows.extend(dict(root='project',relative=name,sha256=digest(changes[name])) for name in FINANCE_INPUTS if name in changes)
        return json.dumps(dict(contract='bounded-mainland-release-v1',deployment_root=str(target),files=rows,
                               old_authorization_reused=False,installation_required=True),indent=2).encode()
    return changed,manifest({}),manifest(changed),sources

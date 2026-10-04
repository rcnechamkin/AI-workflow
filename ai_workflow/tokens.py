"""Where the read-only Linear token comes from: the environment, else the OS secret store.

Never a file in a repository. The token is returned to the caller and not logged, printed or
written anywhere by this module.

    environment      LINEAR_API_KEY
    Windows          Credential Manager, generic credential `ai-workflow-linear`
                     (cmdkey /generic:ai-workflow-linear /user:linear /pass:<token>)
    macOS            Keychain, generic password with service `ai-workflow-linear`
                     (security add-generic-password -s ai-workflow-linear -a linear -w)
    Linux            Secret Service, attribute service=ai-workflow-linear
                     (secret-tool store --label="AI-workflow Linear" service ai-workflow-linear)
"""
import os
import subprocess
import sys

CREDENTIAL = 'ai-workflow-linear'


def _windows(target):
    import ctypes
    from ctypes import wintypes

    class Credential(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                    ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                    ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                    ('UserName', wintypes.LPWSTR)]

    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    found = ctypes.POINTER(Credential)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(found)):          # 1 = CRED_TYPE_GENERIC
        return None
    try:
        blob = ctypes.string_at(found.contents.CredentialBlob, found.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(found)
    return blob.decode('utf-16-le' if b'\x00' in blob else 'utf-8', 'replace')


def _command(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _store(name, platform):
    if platform == 'win32':
        return _windows(name)
    if platform == 'darwin':
        return _command(['security', 'find-generic-password', '-s', name, '-w'])
    return _command(['secret-tool', 'lookup', 'service', name])


def linear_token(env=None, name=CREDENTIAL, platform=None, store=_store):
    """(token, where it came from) or (None, why there is none)."""
    env = os.environ if env is None else env
    if (env.get('LINEAR_API_KEY') or '').strip():
        return env['LINEAR_API_KEY'].strip(), 'environment LINEAR_API_KEY'
    try:
        value = store(name, platform or sys.platform)
    except Exception as e:                       # a broken secret store is "no token", with the reason kept
        return None, f'the OS secret store could not be read ({type(e).__name__})'
    if value and value.strip():
        return value.strip(), f'OS secret store entry {name}'
    return None, f'no LINEAR_API_KEY in the environment and no {name} entry in the OS secret store'

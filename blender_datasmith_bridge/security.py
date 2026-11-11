"""Private local cache helpers. No shell commands or machine identifiers are stored."""
import ctypes
import os
from pathlib import Path
import stat


def local_path(path):
    path = Path(os.path.abspath(path))
    if str(path).startswith(('\\\\', '//')):
        raise ValueError('Use a local cache folder, not a network share')
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            attributes = getattr(part.lstat(), 'st_file_attributes', 0)
            if part.is_symlink() or attributes & 0x400:
                raise ValueError('Cache paths must not contain symbolic links or junctions')
    return path


def private_directory(path):
    path = local_path(path)
    path.mkdir(parents=True, exist_ok=True)
    local_path(path)
    # Windows packaged-app redirection can map LocalAppData to a different
    # physical directory without a junction. Mesh/image paths use resolve(), so
    # the authenticated native input boundary must use the same spelling. Check
    # both the requested path and the resolved path; never resolve away a junction
    # before validating it.
    path = local_path(path.resolve(strict=True))
    if os.name != 'nt':
        path.chmod(stat.S_IRWXU)
        return path
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [w.HLOCAL]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.DWORD)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.c_void_p]
    token = w.HANDLE(); sid_text = w.LPWSTR(); descriptor = ctypes.c_void_p()
    def check(ok):
        if not ok: raise ctypes.WinError(ctypes.get_last_error())
    try:
        check(adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        check(adv.GetTokenInformation(token, 1, data, size, ctypes.byref(size)))
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        check(adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)))
        sddl = f'D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)'
        check(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        check(adv.SetFileSecurityW(str(path), 0x80000004, descriptor))
    finally:
        if descriptor: kernel.LocalFree(descriptor)
        if sid_text: kernel.LocalFree(ctypes.cast(sid_text, w.HLOCAL))
        if token: kernel.CloseHandle(token)
    return path

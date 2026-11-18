"""Antigravity's go-keyring credential on Windows and macOS."""

from __future__ import annotations

import base64
import binascii
import ctypes as ct
import sys

from claude_swap import macos_keychain

from .common import ProviderError

TARGET = "gemini:antigravity"
GENERIC = 1
LOCAL_MACHINE = 2
NOT_FOUND = 1168
KEYCHAIN_PREFIX = "go-keyring-base64:"
SECURITY = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


class Credential(ct.Structure):
    _fields_ = [
        ("Flags", ct.c_uint32), ("Type", ct.c_uint32), ("TargetName", ct.c_wchar_p),
        ("Comment", ct.c_wchar_p), ("LastWritten", ct.c_uint32 * 2),
        ("CredentialBlobSize", ct.c_uint32), ("CredentialBlob", ct.POINTER(ct.c_ubyte)),
        ("Persist", ct.c_uint32), ("AttributeCount", ct.c_uint32),
        ("Attributes", ct.c_void_p), ("TargetAlias", ct.c_wchar_p), ("UserName", ct.c_wchar_p),
    ]


class MemoryStatus(ct.Structure):
    _fields_ = [("length", ct.c_uint32), ("load", ct.c_uint32)] + [
        (name, ct.c_uint64) for name in (
            "total_physical", "available_physical", "total_pagefile", "available_pagefile",
            "total_virtual", "available_virtual", "available_extended_virtual",
        )
    ]


def available_ram() -> int | None:
    if sys.platform != "win32":
        return None
    status = MemoryStatus(length=ct.sizeof(MemoryStatus))
    call = ct.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    call.argtypes = (ct.POINTER(MemoryStatus),)
    call.restype = ct.c_int
    return status.available_physical if call(ct.byref(status)) else None


def _windows_api():
    lib = ct.WinDLL("advapi32", use_last_error=True)
    lib.CredReadW.argtypes = (ct.c_wchar_p, ct.c_uint32, ct.c_uint32, ct.POINTER(ct.POINTER(Credential)))
    lib.CredReadW.restype = ct.c_int
    lib.CredWriteW.argtypes = (ct.POINTER(Credential), ct.c_uint32)
    lib.CredWriteW.restype = ct.c_int
    lib.CredFree.argtypes = (ct.c_void_p,)
    lib.CredFree.restype = None
    return lib


def _read_pointer(api) -> ct.POINTER(Credential):
    result = ct.POINTER(Credential)()
    if not api.CredReadW(TARGET, GENERIC, 0, ct.byref(result)) and ct.get_last_error() != NOT_FOUND:
        raise ProviderError("Cannot read Antigravity from Windows Credential Manager")
    return result


def windows_read() -> bytes | None:
    api = _windows_api()
    entry = _read_pointer(api)
    if not entry:
        return None
    try:
        return ct.string_at(entry.contents.CredentialBlob, entry.contents.CredentialBlobSize)
    finally:
        api.CredFree(entry)


def windows_write(raw: bytes) -> None:
    api = _windows_api()
    current = _read_pointer(api)
    entry = Credential()
    if current:
        ct.memmove(ct.byref(entry), current, ct.sizeof(entry))
    else:
        entry = Credential(Type=GENERIC, TargetName=TARGET, Persist=LOCAL_MACHINE, UserName="antigravity")
    blob = (ct.c_ubyte * len(raw)).from_buffer_copy(raw)
    entry.CredentialBlobSize = len(raw)
    entry.CredentialBlob = ct.cast(blob, ct.POINTER(ct.c_ubyte))
    try:
        if not api.CredWriteW(ct.byref(entry), 0):
            raise ProviderError("Cannot write Antigravity to Windows Credential Manager")
    finally:
        if current:
            api.CredFree(current)


def mac_read() -> bytes | None:
    try:
        value = macos_keychain.get_password("gemini", "antigravity")
        if value is None:
            return None
        if value.startswith(KEYCHAIN_PREFIX):
            return base64.b64decode(value.removeprefix(KEYCHAIN_PREFIX), validate=True)
        return value.encode("utf-8")
    except (*macos_keychain.KEYCHAIN_ERRORS, binascii.Error):
        raise ProviderError("Cannot read Antigravity from the macOS Keychain") from None


def mac_write(raw: bytes) -> None:
    # Keep the credential off process arguments even for large OAuth payloads.
    lib = ct.CDLL(SECURITY)
    core = ct.CDLL(CORE_FOUNDATION)
    query = (ct.c_void_p, ct.c_uint32, ct.c_char_p, ct.c_uint32, ct.c_char_p)
    item_ptr = ct.POINTER(ct.c_void_p)
    lib.SecKeychainFindGenericPassword.argtypes = (*query, ct.c_void_p, ct.c_void_p, item_ptr)
    lib.SecKeychainFindGenericPassword.restype = ct.c_int32
    lib.SecKeychainAddGenericPassword.argtypes = (*query, ct.c_uint32, ct.c_char_p, item_ptr)
    lib.SecKeychainAddGenericPassword.restype = ct.c_int32
    lib.SecKeychainItemModifyAttributesAndData.argtypes = (ct.c_void_p, ct.c_void_p, ct.c_uint32, ct.c_char_p)
    lib.SecKeychainItemModifyAttributesAndData.restype = ct.c_int32
    core.CFRelease.argtypes = (ct.c_void_p,)
    core.CFRelease.restype = None
    value = KEYCHAIN_PREFIX.encode() + base64.b64encode(raw)
    args = (None, len(b"gemini"), b"gemini", len(b"antigravity"), b"antigravity")
    item = ct.c_void_p()
    result = lib.SecKeychainFindGenericPassword(*args, None, None, ct.byref(item))
    if result == -25300:  # errSecItemNotFound
        result = lib.SecKeychainAddGenericPassword(*args, len(value), value, None)
    elif result == 0:
        try:
            result = lib.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
        finally:
            core.CFRelease(item)
    if result:
        raise ProviderError("Cannot write Antigravity to the macOS Keychain")

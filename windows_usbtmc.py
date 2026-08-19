"""Native Windows USBTMC transport for the IVI ausbtmc.sys class driver.

This bypasses VISA resource discovery.  It is useful when Device Manager shows
"USB Test and Measurement Device (IVI)" but NI-VISA list_resources() is empty.
"""
from __future__ import annotations

import ctypes
import re
import struct
import sys
from dataclasses import dataclass
from ctypes import wintypes


USBTMC_GUID_TEXT = "{A9FDBB24-128A-11D5-9961-00108335E361}"
_GUID_BYTES = bytes.fromhex("24bbfda98a12d511996100108335e361")
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


@dataclass(frozen=True)
class UsbTmcDeviceInfo:
    path: str
    vid: int
    pid: int
    serial: str

    @property
    def resource(self) -> str:
        return f"USB0::0x{self.vid:04X}::0x{self.pid:04X}::{self.serial}::INSTR"


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows 原生 USBTMC 后端只能在 Windows 上使用。")


def list_usbtmc_devices() -> list[UsbTmcDeviceInfo]:
    """Enumerate present USBTMC interfaces through SetupAPI."""
    _require_windows()
    guid_type = ctypes.c_ubyte * 16
    guid = guid_type.from_buffer_copy(_GUID_BYTES)
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

    class InterfaceData(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("InterfaceClassGuid", guid_type),
            ("Flags", wintypes.DWORD),
            ("Reserved", ctypes.c_void_p),
        ]

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(guid_type), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(guid_type),
        wintypes.DWORD, ctypes.POINTER(InterfaceData),
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(InterfaceData), ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    present_and_interfaces = 0x2 | 0x10
    no_more_items = 259
    devset = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, present_and_interfaces
    )
    invalid = wintypes.HANDLE(-1).value
    if devset == invalid:
        raise ctypes.WinError(ctypes.get_last_error())

    results: list[UsbTmcDeviceInfo] = []
    try:
        index = 0
        while True:
            interface = InterfaceData()
            interface.cbSize = ctypes.sizeof(interface)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                devset, None, ctypes.byref(guid), index, ctypes.byref(interface)
            ):
                error = ctypes.get_last_error()
                if error == no_more_items:
                    break
                raise ctypes.WinError(error)

            needed = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                devset, ctypes.byref(interface), None, 0, ctypes.byref(needed), None
            )
            buffer = ctypes.create_string_buffer(needed.value)
            # cbSize is 8 on 64-bit and 6 on 32-bit Windows; DevicePath begins
            # at byte offset 4 in both packed layouts.
            ctypes.cast(buffer, ctypes.POINTER(wintypes.DWORD))[0] = (
                8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            )
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                devset, ctypes.byref(interface), buffer, needed, None, None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
            match = re.search(
                r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})#([^#]+)#",
                path,
                re.IGNORECASE,
            )
            if match:
                results.append(
                    UsbTmcDeviceInfo(
                        path=path,
                        vid=int(match.group(1), 16),
                        pid=int(match.group(2), 16),
                        serial=match.group(3).upper(),
                    )
                )
            index += 1
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devset)
    return results


class WindowsUsbTmc:
    """Small USBTMC/USB488 message transport over IVI ausbtmc.sys."""

    def __init__(self, path: str) -> None:
        _require_windows()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        self._handle = self._kernel32.CreateFileW(
            path,
            0xC0000000,  # GENERIC_READ | GENERIC_WRITE
            0x1 | 0x2,   # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,           # OPEN_EXISTING
            FILE_FLAG_OVERLAPPED,
            None,
        )
        if self._handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self._tag = 0

    def _configure_api(self) -> None:
        k32 = self._kernel32
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        k32.WriteFile.restype = wintypes.BOOL
        k32.ReadFile.argtypes = k32.WriteFile.argtypes
        k32.ReadFile.restype = wintypes.BOOL
        k32.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        k32.GetOverlappedResult.restype = wintypes.BOOL
        k32.CancelIoEx.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_Overlapped),
        ]
        k32.CancelIoEx.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

    def _next_tag(self) -> int:
        self._tag = self._tag % 255 + 1
        return self._tag

    def _overlapped_io(
        self,
        operation,
        buffer,
        size: int,
        timeout_ms: int,
        description: str,
    ) -> int:
        event = self._kernel32.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        overlapped = _Overlapped()
        overlapped.hEvent = event
        transferred = wintypes.DWORD()
        try:
            ctypes.set_last_error(0)
            completed = operation(
                self._handle,
                buffer,
                size,
                ctypes.byref(transferred),
                ctypes.byref(overlapped),
            )
            if not completed:
                error = ctypes.get_last_error()
                if error != ERROR_IO_PENDING:
                    raise ctypes.WinError(error)
                wait_result = self._kernel32.WaitForSingleObject(event, timeout_ms)
                if wait_result == WAIT_TIMEOUT:
                    self._kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                    self._kernel32.WaitForSingleObject(event, 1000)
                    raise TimeoutError(
                        f"USBTMC {description}超时（{timeout_ms / 1000:g} 秒）；"
                        "请重新连接 USB 后重试。"
                    )
                if wait_result != WAIT_OBJECT_0:
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self._kernel32.GetOverlappedResult(
                self._handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                False,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            return transferred.value
        finally:
            self._kernel32.CloseHandle(event)

    def _write_raw(self, packet: bytes, timeout_ms: int = 3000) -> None:
        written = self._overlapped_io(
            self._kernel32.WriteFile,
            packet,
            len(packet),
            timeout_ms,
            "写入",
        )
        if written != len(packet):
            raise OSError(f"USBTMC 写入不完整：{written}/{len(packet)} 字节")

    def _read_raw(self, maximum: int = 1_048_576, timeout_ms: int = 8000) -> bytes:
        buffer = ctypes.create_string_buffer(maximum + 12)
        received = self._overlapped_io(
            self._kernel32.ReadFile,
            buffer,
            len(buffer),
            timeout_ms,
            "读取",
        )
        return buffer.raw[:received]

    def write(self, command: str) -> None:
        payload = command.rstrip("\r\n").encode("ascii") + b"\n"
        tag = self._next_tag()
        header = struct.pack(
            "<BBBBIBBBB", 1, tag, tag ^ 0xFF, 0, len(payload), 1, 0, 0, 0
        )
        packet = header + payload
        packet += b"\0" * ((-len(packet)) % 4)
        self._write_raw(packet)

    def query(self, command: str, maximum: int = 1_048_576) -> str:
        self.write(command)
        tag = self._next_tag()
        request = struct.pack(
            "<BBBBIBBBB", 2, tag, tag ^ 0xFF, 0, maximum, 0, 0, 0, 0
        )
        self._write_raw(request)
        chunks: list[bytes] = []
        while True:
            packet = self._read_raw(maximum)
            if len(packet) < 12 or packet[0] != 2 or packet[1] != tag:
                raise OSError("收到无效的 USBTMC 响应包。")
            size = struct.unpack_from("<I", packet, 4)[0]
            chunks.append(packet[12 : 12 + size])
            if packet[8] & 1:
                break
        return b"".join(chunks).decode("ascii", "replace").strip()

    def close(self) -> None:
        if getattr(self, "_handle", None) not in (None, wintypes.HANDLE(-1).value):
            self._kernel32.CancelIoEx(self._handle, None)
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "WindowsUsbTmc":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

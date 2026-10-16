r"""直连 Windows SetupAPI：注册一个 root 枚举的 Net 设备，并用系统自带的 INF 装驱动。

这是 devcon 当年 `devcon install netloop.inf *MSLOOP` 做的同一件事，只是搬进进程内，
不依赖 devcon（微软早就不随系统发了），也不引入任何第三方驱动。

**必须管理员**：注册设备与装驱动都要求提权，调用方负责先提权（见 `cli._with_elevation`）。

为什么不是 PowerShell + Add-Type：那样要现场编译一次 C#，出错的形态更难诊断；
ctypes 直接按结构体布局调，失败时能拿到 `GetLastError` 的原文。

结构体布局是本文件最容易出错的地方，所以下面把 offset 写在注释里，并有一条
`test_devinfo_layout_matches_native` 钉住它——布局错了 `SetupDiCreateDeviceInfo` 会
返回一个莫名其妙的参数错误，那种错在现场极难查。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from .errors import NetError

NET_CLASS_GUID = "{4d36e972-e325-11ce-bfc1-08002be10318}"
LOOPBACK_HARDWARE_ID = "*MSLOOP"

SPDRP_HARDWAREID = 0x00000001
DIF_REGISTERDEVICE = 0x00000019
DICD_GENERATE_ID = 0x00000001
SPDIT_COMPATDRIVER = 0x00000002
LINE_LEN = 256

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),          # 0
                ("Data2", wintypes.WORD),           # 4
                ("Data3", wintypes.WORD),           # 6
                ("Data4", ctypes.c_ubyte * 8)]      # 8  → 共 16 字节，对齐 4


class SP_DEVINFO_DATA(ctypes.Structure):
    # 原生布局（x64）：cbSize@0、ClassGuid@4、DevInst@20、Reserved@24，共 32 字节。
    # Reserved 是 ULONG_PTR，按 8 字节对齐，所以 @24 正好，不需要显式 padding。
    _fields_ = [("cbSize", wintypes.DWORD),
                ("ClassGuid", GUID),
                ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class SP_DRVINFO_DATA_V2_W(ctypes.Structure):
    """设备可用的驱动信息。布局按 MSDN：三个 LINE_LEN 宽字符数组要留满 256。"""

    _fields_ = [("cbSize", wintypes.DWORD),
                ("DriverType", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p),
                ("Description", wintypes.WCHAR * LINE_LEN),
                ("MfgName", wintypes.WCHAR * LINE_LEN),
                ("ProviderName", wintypes.WCHAR * LINE_LEN),
                ("DriverDate", wintypes.FILETIME),
                ("DriverVersion", ctypes.c_ulonglong)]


def guid_from_string(text: str) -> GUID:
    """`{4d36e972-e325-11ce-bfc1-08002be10318}` -> GUID。手写解析，不依赖 ole32。"""
    try:
        body = text.strip().strip("{}")
        parts = body.split("-")
        if len(parts) != 5:
            raise ValueError("段数不对")
        data4 = bytes.fromhex(parts[3] + parts[4])
        if len(data4) != 8:
            raise ValueError("末段长度不对")
        return GUID(Data1=int(parts[0], 16), Data2=int(parts[1], 16),
                    Data3=int(parts[2], 16),
                    Data4=(ctypes.c_ubyte * 8).from_buffer_copy(data4))
    except Exception as exc:
        raise NetError(f"GUID {text!r} 解析失败：{exc}") from exc


def _require_windows() -> None:
    if sys.platform != "win32":
        raise NetError("创建网卡只在 Windows 上可用")


def _dlls():
    try:
        setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        newdev = ctypes.WinDLL("newdev", use_last_error=True)
    except OSError as exc:
        raise NetError(f"加载系统 DLL 失败：{exc}") from exc
    return setupapi, newdev


def _bind(setupapi, newdev) -> None:
    h = wintypes.HANDLE
    setupapi.SetupDiCreateDeviceInfoList.argtypes = [ctypes.POINTER(GUID), wintypes.HWND]
    setupapi.SetupDiCreateDeviceInfoList.restype = h
    setupapi.SetupDiCreateDeviceInfoW.argtypes = [
        h, wintypes.LPCWSTR, ctypes.POINTER(GUID), wintypes.LPCWSTR,
        wintypes.HWND, wintypes.DWORD, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiCreateDeviceInfoW.restype = wintypes.BOOL
    setupapi.SetupDiSetDeviceRegistryPropertyW.argtypes = [
        h, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD]
    setupapi.SetupDiSetDeviceRegistryPropertyW.restype = wintypes.BOOL
    setupapi.SetupDiCallClassInstaller.argtypes = [
        wintypes.DWORD, h, ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiCallClassInstaller.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [h]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        h, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = wintypes.BOOL
    setupapi.SetupDiBuildDriverInfoList.argtypes = [
        h, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD]
    setupapi.SetupDiBuildDriverInfoList.restype = wintypes.BOOL
    setupapi.SetupDiEnumDriverInfoW.argtypes = [
        h, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(SP_DRVINFO_DATA_V2_W)]
    setupapi.SetupDiEnumDriverInfoW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDriverInfoList.argtypes = [
        h, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.DWORD]
    setupapi.SetupDiDestroyDriverInfoList.restype = wintypes.BOOL
    newdev.DiInstallDevice.argtypes = [
        wintypes.HWND, h, ctypes.POINTER(SP_DEVINFO_DATA),
        ctypes.POINTER(SP_DRVINFO_DATA_V2_W), wintypes.DWORD,
        ctypes.POINTER(wintypes.BOOL)]
    newdev.DiInstallDevice.restype = wintypes.BOOL


def _why(prefix: str) -> NetError:
    code = ctypes.get_last_error()
    try:
        return NetError(f"{prefix}（错误 {code}：{ctypes.FormatError(code)}）")
    except Exception:                      # noqa: BLE001
        # FormatError 取不到就只说错误码：错误码本身才是现场要的那一条
        return NetError(f"{prefix}（错误 {code}）")


def _device_instance_id(setupapi, info_set, info) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD(0)
    if not setupapi.SetupDiGetDeviceInstanceIdW(
            info_set, ctypes.byref(info), buffer, 256, ctypes.byref(needed)):
        return ""
    return buffer.value


def create_loopback_device(inf_path: Path,
                           hardware_id: str = LOOPBACK_HARDWARE_ID):
    """注册 root Net 设备并把驱动按**设备实例**装上。返回 (实例ID, 是否需要重启)。

    这里刻意**不用** `UpdateDriverForPlugAndPlayDevices`：它按硬件 ID 找设备，而现场的
    KM-TEST 环回适配器硬件 ID 全是 `*MSLOOP`，于是它会把驱动重装到**别人那块**已存在的
    网卡上（实测：本机"以太网 5"的描述被从中文改成了英文，新设备反而没装上驱动）。
    改成 `DiInstallDevice`，只认我们刚注册的那个设备实例。
    """
    _require_windows()
    if not Path(inf_path).is_file():
        raise NetError(f"找不到 INF：{inf_path}")
    setupapi, newdev = _dlls()
    _bind(setupapi, newdev)

    class_guid = guid_from_string(NET_CLASS_GUID)
    info_set = setupapi.SetupDiCreateDeviceInfoList(ctypes.byref(class_guid), None)
    if info_set in (None, INVALID_HANDLE_VALUE):
        raise _why("创建设备列表失败")
    try:
        info = SP_DEVINFO_DATA()
        info.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
        if not setupapi.SetupDiCreateDeviceInfoW(
                info_set, hardware_id, ctypes.byref(class_guid), None, None,
                DICD_GENERATE_ID, ctypes.byref(info)):
            raise _why("注册设备节点失败")
        # REG_MULTI_SZ：硬件 ID + 双 NUL 结尾
        buffer = (hardware_id + "\0\0").encode("utf-16-le")
        if not setupapi.SetupDiSetDeviceRegistryPropertyW(
                info_set, ctypes.byref(info), SPDRP_HARDWAREID,
                buffer, len(buffer)):
            raise _why("写入硬件 ID 失败")
        if not setupapi.SetupDiCallClassInstaller(
                DIF_REGISTERDEVICE, info_set, ctypes.byref(info)):
            raise _why("注册设备失败（可能需要管理员权限）")

        instance = _device_instance_id(setupapi, info_set, info)
        if not instance:
            raise _why("拿不到新设备的实例 ID")

        if not setupapi.SetupDiBuildDriverInfoList(
                info_set, ctypes.byref(info), SPDIT_COMPATDRIVER):
            raise _why(f"{instance} 构建驱动列表失败")
        try:
            driver = SP_DRVINFO_DATA_V2_W()
            driver.cbSize = ctypes.sizeof(SP_DRVINFO_DATA_V2_W)
            if not setupapi.SetupDiEnumDriverInfoW(
                    info_set, ctypes.byref(info), SPDIT_COMPATDRIVER, 0,
                    ctypes.byref(driver)):
                raise _why(f"{instance} 的驱动列表里没有可用项（INF 没被识别？）")
            reboot = wintypes.BOOL(False)
            if not newdev.DiInstallDevice(
                    None, info_set, ctypes.byref(info), ctypes.byref(driver),
                    0, ctypes.byref(reboot)):
                raise _why(f"在 {instance} 上安装驱动失败")
            return instance, bool(reboot.value)
        finally:
            ctypes.set_last_error(0)
            setupapi.SetupDiDestroyDriverInfoList(
                info_set, ctypes.byref(info), SPDIT_COMPATDRIVER)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)


def remove_device(instance_id: str) -> str:
    """用系统自带的 pnputil 移除设备实例。返回 pnputil 的输出（失败时附在异常里）。"""
    _require_windows()
    if not instance_id:
        raise NetError("没有给设备实例 ID")
    try:
        proc = subprocess.run(["pnputil", "/remove-device", instance_id],
                              capture_output=True, timeout=120)
    except FileNotFoundError as exc:
        raise NetError("找不到 pnputil") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetError("移除设备超时") from exc
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise NetError(f"移除设备失败（退出码 {proc.returncode}）：{out}")
    return out

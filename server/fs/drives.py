"""Список дисков сервера и свойства их файловых систем.

Аналог DriveInfo.GetDrives() из .NET. Поля ответа совпадают по смыслу:

    name             Name               имя диска или точка монтирования
    type             DriveType          fixed, removable, network, cdrom, ram
    filesystem       DriveFormat        NTFS, ext4, ...
    label            VolumeLabel        метка тома
    ready            IsReady            готов ли диск (вставлен ли DVD)
    total_size       TotalSize          объём в байтах
    total_free       TotalFreeSpace     свободно всего
    available_free   AvailableFreeSpace свободно для текущего пользователя

Последние два отличаются: на Linux часть места зарезервирована для root,
на Windows действуют дисковые квоты.

Стандартная библиотека Python не умеет перечислять диски и узнавать тип
файловой системы, поэтому на Windows используются функции WinAPI через
ctypes, а на Linux — /proc/mounts и statvfs.

Список дисков — сведения об устройстве сервера, и выдаются они только
вошедшим пользователям. Пути на дисках при этом недоступны: файловые
операции работают только в каталоге пользователя.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

# Файловые системы Linux, которые не являются хранилищами: proc, sysfs и
# прочие виртуальные. Показывать их — засорять ответ десятками строк.
_PSEUDO_FS = frozenset(
    {
        "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs",
        "devpts", "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "nsfs",
        "overlay", "proc", "pstore", "rpc_pipefs", "securityfs", "squashfs", "sysfs",
        "tracefs", "fuse.gvfsd-fuse", "fuse.portal", "ramfs",
    }
)
_NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs"})


def list_drives() -> list[dict[str, Any]]:
    if sys.platform == "win32":
        return _windows_drives()
    if Path("/proc/mounts").exists():
        return _linux_drives()
    return [_generic_drive("/")]


# --- Windows ---


def _windows_drives() -> list[dict[str, Any]]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDrives.restype = wintypes.DWORD
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.GetDiskFreeSpaceExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ]
    kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    kernel32.SetThreadErrorMode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.SetThreadErrorMode.restype = wintypes.BOOL

    drive_types = {
        2: "removable", 3: "fixed", 4: "network", 5: "cdrom", 6: "ram",
    }
    # Без этого обращение к пустому картридеру или DVD показывает на сервере
    # модальное окно «Вставьте диск» и подвешивает поток до нажатия кнопки.
    SEM_FAILCRITICALERRORS = 0x0001
    previous = wintypes.DWORD()
    kernel32.SetThreadErrorMode(SEM_FAILCRITICALERRORS, ctypes.byref(previous))

    drives: list[dict[str, Any]] = []
    try:
        mask = kernel32.GetLogicalDrives()
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            info: dict[str, Any] = {
                "name": root,
                "type": drive_types.get(kernel32.GetDriveTypeW(root), "unknown"),
                "filesystem": None,
                "label": None,
                "ready": False,
                "total_size": None,
                "total_free": None,
                "available_free": None,
            }

            label = ctypes.create_unicode_buffer(261)
            filesystem = ctypes.create_unicode_buffer(261)
            if kernel32.GetVolumeInformationW(
                root, label, len(label), None, None, None, filesystem, len(filesystem)
            ):
                info["ready"] = True
                info["label"] = label.value
                info["filesystem"] = filesystem.value

                available = ctypes.c_ulonglong()
                total = ctypes.c_ulonglong()
                free = ctypes.c_ulonglong()
                if kernel32.GetDiskFreeSpaceExW(
                    root, ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)
                ):
                    info["total_size"] = total.value
                    info["total_free"] = free.value
                    info["available_free"] = available.value
            drives.append(info)
    finally:
        kernel32.SetThreadErrorMode(previous.value, None)
    return drives


# --- Linux ---


def _linux_drives() -> list[dict[str, Any]]:
    labels = _linux_labels()
    drives: list[dict[str, Any]] = []
    seen: set[str] = set()

    with open("/proc/mounts", encoding="utf-8", errors="replace") as mounts:
        for line in mounts:
            fields = line.split()
            if len(fields) < 3:
                continue
            device, mountpoint, fstype = (_unescape(field) for field in fields[:3])
            if fstype in _PSEUDO_FS or mountpoint in seen:
                continue
            # tmpfs показывается только для /tmp и /dev/shm — остальные
            # экземпляры служебные и только засоряют список.
            if fstype == "tmpfs" and mountpoint not in {"/tmp", "/dev/shm"}:
                continue
            if not device.startswith("/") and fstype not in _NETWORK_FS and fstype != "tmpfs":
                continue
            seen.add(mountpoint)

            info: dict[str, Any] = {
                "name": mountpoint,
                "type": _linux_type(device, fstype),
                "filesystem": fstype,
                "label": labels.get(os.path.realpath(device)),
                "ready": False,
                "total_size": None,
                "total_free": None,
                "available_free": None,
            }
            try:
                st = os.statvfs(mountpoint)
            except OSError:
                drives.append(info)
                continue
            info["ready"] = True
            info["total_size"] = st.f_blocks * st.f_frsize
            info["total_free"] = st.f_bfree * st.f_frsize
            info["available_free"] = st.f_bavail * st.f_frsize
            drives.append(info)
    return drives


def _linux_labels() -> dict[str, str]:
    """Устройство -> метка тома по ссылкам в /dev/disk/by-label."""
    labels: dict[str, str] = {}
    directory = Path("/dev/disk/by-label")
    try:
        for link in directory.iterdir():
            labels[os.path.realpath(link)] = _unescape(link.name)
    except OSError:
        pass
    return labels


def _linux_type(device: str, fstype: str) -> str:
    if fstype in _NETWORK_FS:
        return "network"
    if fstype == "tmpfs":
        return "ram"
    if fstype in {"iso9660", "udf"}:
        return "cdrom"
    name = Path(os.path.realpath(device)).name
    # Для раздела sdb1 признак съёмности лежит у устройства sdb.
    for candidate in (name, name.rstrip("0123456789")):
        flag = Path("/sys/block") / candidate / "removable"
        try:
            return "removable" if flag.read_text().strip() == "1" else "fixed"
        except OSError:
            continue
    return "fixed"


def _unescape(field: str) -> str:
    # В /proc/mounts пробелы и табуляции в путях записаны восьмеричными
    # последовательностями: «/media/My Disk» хранится как /media/My\040Disk.
    for code, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        field = field.replace(code, char)
    return field


def _generic_drive(root: str) -> dict[str, Any]:
    usage = shutil.disk_usage(root)
    return {
        "name": root,
        "type": "fixed",
        "filesystem": None,
        "label": None,
        "ready": True,
        "total_size": usage.total,
        "total_free": usage.free,
        "available_free": usage.free,
    }

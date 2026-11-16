# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""
Minimal Mach-O load-command reader.

Only what the bundler needs: the file type, the install name (LC_ID_DYLIB), the
linked libraries and the LC_RPATH entries. Thin and fat (universal) files of either
byte order are handled; for fat files the commands of all slices are merged.

Parsing the headers directly instead of scraping `otool -L` keeps the install name
apart from the dependencies (otool prints both in one list, and for MH_BUNDLE files
there is no ID line at all, so "skip the first line" heuristics drop a real dep),
exposes LC_RPATH, and doubles as a reliable "is this a Mach-O file?" test.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF

MH_EXECUTE = 0x2
MH_DYLIB = 0x6
MH_BUNDLE = 0x8

LC_REQ_DYLD = 0x80000000
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_RPATH = 0x1C | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_LAZY_LOAD_DYLIB = 0x20
LC_LOAD_UPWARD_DYLIB = 0x23 | LC_REQ_DYLD

DEP_COMMANDS = {
    LC_LOAD_DYLIB,
    LC_LOAD_WEAK_DYLIB,
    LC_REEXPORT_DYLIB,
    LC_LAZY_LOAD_DYLIB,
    LC_LOAD_UPWARD_DYLIB,
}

# Java class files share 0xCAFEBABE; a real fat header never has this many slices.
_MAX_FAT_ARCHS = 64


class MachOError(ValueError):
    pass


@dataclass(frozen=True)
class MachO:
    filetype: int
    install_name: str | None
    deps: tuple[str, ...]  # ordered, de-duplicated across slices
    weak_deps: frozenset[str]  # subset of deps that may legitimately be absent
    rpaths: tuple[
        str, ...
    ]  # in load order, duplicates kept (install_name_tool needs them)

    @property
    def is_dylib(self) -> bool:
        return self.filetype == MH_DYLIB


def _cstr(blob: bytes, offset: int) -> str:
    end = blob.find(b"\0", offset)
    if end == -1:
        end = len(blob)
    return blob[offset:end].decode("utf-8", "surrogateescape")


def _parse_thin(data: bytes, base: int) -> tuple[int, str | None, list, list]:
    if len(data) < base + 28:
        raise MachOError("truncated header")
    magic_le = struct.unpack_from("<I", data, base)[0]
    if magic_le in (MH_MAGIC, MH_MAGIC_64):
        endian = "<"
    elif struct.unpack_from(">I", data, base)[0] in (MH_MAGIC, MH_MAGIC_64):
        endian = ">"
    else:
        raise MachOError("not a Mach-O slice")
    magic = struct.unpack_from(endian + "I", data, base)[0]
    _, _, _, filetype, ncmds, sizeofcmds, _ = struct.unpack_from(
        endian + "7I", data, base
    )
    off = base + (32 if magic == MH_MAGIC_64 else 28)
    end = off + sizeofcmds
    if end > len(data):
        raise MachOError("load commands run past end of file")

    install_name: str | None = None
    deps: list[tuple[str, bool]] = []
    rpaths: list[str] = []
    for _ in range(ncmds):
        if off + 8 > end:
            raise MachOError("truncated load command")
        cmd, cmdsize = struct.unpack_from(endian + "2I", data, off)
        if cmdsize < 8 or off + cmdsize > end:
            raise MachOError("bad load command size")
        blob = data[off : off + cmdsize]
        if cmd == LC_ID_DYLIB or cmd in DEP_COMMANDS or cmd == LC_RPATH:
            (str_off,) = struct.unpack_from(endian + "I", blob, 8)
            value = _cstr(blob, str_off)
            if cmd == LC_ID_DYLIB:
                install_name = value
            elif cmd == LC_RPATH:
                rpaths.append(value)
            else:
                deps.append((value, cmd == LC_LOAD_WEAK_DYLIB))
        off += cmdsize
    return filetype, install_name, deps, rpaths


def parse_macho(data: bytes) -> MachO:
    """Parse a Mach-O image held in memory. Raises MachOError if it is not one."""
    if len(data) < 8:
        raise MachOError("too short")
    slices: list[int]
    (magic_be,) = struct.unpack_from(">I", data, 0)
    if magic_be in (FAT_MAGIC, FAT_MAGIC_64):
        (nfat,) = struct.unpack_from(">I", data, 4)
        if not 0 < nfat <= _MAX_FAT_ARCHS:
            raise MachOError("implausible fat header")
        entry, fmt = (20, ">5I") if magic_be == FAT_MAGIC else (32, ">2I2QII")
        slices = []
        for i in range(nfat):
            pos = 8 + i * entry
            if pos + entry > len(data):
                raise MachOError("truncated fat header")
            slices.append(int(struct.unpack_from(fmt, data, pos)[2]))
    else:
        slices = [0]

    filetype = None
    install_name = None
    deps: dict[str, bool] = {}  # name -> weak in every slice
    rpaths: list[str] = []
    for base in slices:
        ft, iname, sdeps, srpaths = _parse_thin(data, base)
        filetype = ft if filetype is None else filetype
        install_name = install_name or iname
        for name, weak in sdeps:
            deps[name] = deps.get(name, True) and weak
        if not rpaths:
            rpaths = list(srpaths)  # keep per-slice duplicates
        else:
            rpaths += [r for r in srpaths if r not in rpaths]

    return MachO(
        filetype=filetype if filetype is not None else 0,
        install_name=install_name,
        deps=tuple(deps),
        weak_deps=frozenset(n for n, weak in deps.items() if weak),
        rpaths=tuple(rpaths),
    )


def read_macho(path: Path) -> MachO | None:
    """Mach-O info for `path`, or None if it is unreadable or not a Mach-O file."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            if len(head) < 4 or struct.unpack(">I", head)[0] not in (
                MH_MAGIC,
                MH_MAGIC_64,
                FAT_MAGIC,
                FAT_MAGIC_64,
                0xCEFAEDFE,  # MH_MAGIC, little-endian on disk
                0xCFFAEDFE,  # MH_MAGIC_64, little-endian on disk
            ):
                return None
            data = head + fh.read()
        return parse_macho(data)
    except (OSError, MachOError, struct.error):
        return None

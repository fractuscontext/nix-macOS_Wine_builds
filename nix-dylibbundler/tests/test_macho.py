# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

import struct
import tempfile
import unittest
from pathlib import Path

from nix_dylibbundler.macho import (
    LC_ID_DYLIB,
    LC_LOAD_DYLIB,
    LC_LOAD_WEAK_DYLIB,
    LC_RPATH,
    MH_BUNDLE,
    MH_DYLIB,
    MH_MAGIC_64,
    MachOError,
    parse_macho,
    read_macho,
)


def _str_cmd(cmd: int, value: str, header: int) -> bytes:
    raw = value.encode() + b"\0"
    size = (header + len(raw) + 7) & ~7
    fixed = struct.pack("<3I", cmd, size, header)
    if header == 24:  # dylib_command: timestamp, current, compatibility version
        fixed += struct.pack("<3I", 2, 0x10000, 0x10000)
    return (fixed + raw).ljust(size, b"\0")


def thin(filetype=MH_DYLIB, install_name=None, deps=(), weak=(), rpaths=()) -> bytes:
    cmds = []
    if install_name:
        cmds.append(_str_cmd(LC_ID_DYLIB, install_name, 24))
    cmds += [_str_cmd(LC_LOAD_DYLIB, d, 24) for d in deps]
    cmds += [_str_cmd(LC_LOAD_WEAK_DYLIB, d, 24) for d in weak]
    cmds += [_str_cmd(LC_RPATH, r, 12) for r in rpaths]
    body = b"".join(cmds)
    header = struct.pack(
        "<8I", MH_MAGIC_64, 0x01000007, 3, filetype, len(cmds), len(body), 0, 0
    )
    return header + body


def fat(*slices: bytes) -> bytes:
    align = 0x1000
    head = struct.pack(">2I", 0xCAFEBABE, len(slices))
    offset = align
    entries, blobs = b"", b""
    for s in slices:
        entries += struct.pack(">5I", 0x01000007, 3, offset, len(s), 12)
        blobs += s.ljust(align, b"\0")
        offset += align
    return (head + entries).ljust(align, b"\0") + blobs


class TestParse(unittest.TestCase):
    def test_thin_dylib(self):
        m = parse_macho(
            thin(
                install_name="/nix/store/h-a/lib/liba.dylib",
                deps=["/usr/lib/libSystem.B.dylib", "@rpath/libb.dylib"],
                weak=["/nix/store/h-c/lib/libc_.dylib"],
                rpaths=["@loader_path/", "/nix/store/h-b/lib"],
            )
        )
        self.assertTrue(m.is_dylib)
        self.assertEqual(m.install_name, "/nix/store/h-a/lib/liba.dylib")
        # the install name is NOT reported as a dependency (otool -L mixes them)
        self.assertEqual(
            m.deps,
            (
                "/usr/lib/libSystem.B.dylib",
                "@rpath/libb.dylib",
                "/nix/store/h-c/lib/libc_.dylib",
            ),
        )
        self.assertEqual(m.weak_deps, {"/nix/store/h-c/lib/libc_.dylib"})
        self.assertEqual(m.rpaths, ("@loader_path/", "/nix/store/h-b/lib"))

    def test_bundle_has_no_install_name(self):
        m = parse_macho(thin(filetype=MH_BUNDLE, deps=["/x/libfirst.dylib"]))
        self.assertIsNone(m.install_name)
        self.assertEqual(m.deps, ("/x/libfirst.dylib",))  # first dep is not dropped

    def test_fat_merges_slices(self):
        m = parse_macho(
            fat(thin(deps=["/a.dylib"]), thin(deps=["/a.dylib", "/b.dylib"]))
        )
        self.assertEqual(m.deps, ("/a.dylib", "/b.dylib"))

    def test_garbage(self):
        for blob in (b"", b"\x7fELF" + b"\0" * 60, struct.pack(">2I", 0xCAFEBABE, 0)):
            with self.assertRaises(MachOError):
                parse_macho(blob)

    def test_read_macho_rejects_non_macho_files(self):
        with tempfile.TemporaryDirectory() as d:
            text = Path(d, "libfoo.dylib")
            text.write_text("INPUT(-lfoo)\n")  # e.g. a linker script named like a lib
            self.assertIsNone(read_macho(text))
            real = Path(d, "real.dylib")
            real.write_bytes(thin(install_name="@rpath/real.dylib"))
            self.assertEqual(read_macho(real).install_name, "@rpath/real.dylib")


if __name__ == "__main__":
    unittest.main()

# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import typer

from nix_dylibbundler.cli import (
    AppConfig,
    DylibBundler,
    MacToolchain,
    TargetNamer,
    is_dynamic_library,
    is_system_lib,
    nix_store_hash,
    verify_bundle,
)
from nix_dylibbundler.macho import MH_BUNDLE, MH_DYLIB, MH_EXECUTE, MachO

FAKE_MAGIC = b"FAKEMACHO\n"


def write_fake(
    path: Path, deps=(), rpaths=(), install_name=None, filetype=MH_DYLIB, weak=()
):
    """A stand-in Mach-O: the fake toolchain reads and rewrites it like the real one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "filetype": filetype,
        "id": install_name
        if install_name is not None
        else (str(path) if filetype == MH_DYLIB else None),
        "deps": list(deps),
        "weak": list(weak),
        "rpaths": list(rpaths),
    }
    path.write_bytes(FAKE_MAGIC + json.dumps(doc).encode())
    return path


class FakeToolchain:
    def __init__(self):
        self.rewrites: list[dict] = []

    def _load(self, binary: Path) -> dict | None:
        data = binary.read_bytes()
        if not data.startswith(FAKE_MAGIC):
            return None
        return json.loads(data[len(FAKE_MAGIC) :])

    def inspect(self, binary: Path) -> MachO | None:
        doc = self._load(binary)
        if doc is None:
            return None
        return MachO(
            filetype=doc["filetype"],
            install_name=doc["id"],
            deps=tuple(doc["deps"]),
            weak_deps=frozenset(doc["weak"]),
            rpaths=tuple(doc["rpaths"]),
        )

    def rewrite(self, binary, changes, new_id=None, delete_rpaths=()):
        changes, delete_rpaths = list(changes), list(delete_rpaths)
        self.rewrites.append(
            {
                "binary": binary,
                "changes": changes,
                "new_id": new_id,
                "drop": delete_rpaths,
            }
        )
        doc = self._load(binary)
        mapping = dict(changes)
        doc["deps"] = [mapping.get(d, d) for d in doc["deps"]]
        for r in delete_rpaths:
            doc["rpaths"].remove(r)
        if new_id:
            doc["id"] = new_id
        binary.write_bytes(FAKE_MAGIC + json.dumps(doc).encode())
        return []


class TestHelpers(unittest.TestCase):
    def test_is_dynamic_library(self):
        for name in ("libfoo.dylib", "libfoo.so", "libfoo.so.1.2.3", "libfoo.1.dylib"):
            self.assertTrue(is_dynamic_library(Path(name)), name)
        for name in ("libfoo.a", "binary_exe", "libfoo.dylib.dSYM"):
            self.assertFalse(is_dynamic_library(Path(name)), name)

    def test_is_system_lib(self):
        self.assertTrue(is_system_lib("/usr/lib/libSystem.B.dylib"))
        self.assertTrue(
            is_system_lib(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        )
        self.assertTrue(is_system_lib("/nix/store/hash-libcxx/lib/libc++.1.dylib"))
        self.assertTrue(
            is_system_lib(
                "/nix/store/hash-apple-framework-CoreData/Library/Frameworks/CoreData"
            )
        )
        self.assertFalse(
            is_system_lib("/nix/store/hash-gstreamer-1.0/lib/libgstreamer.dylib")
        )
        # anchored prefix: a store path that merely contains /usr/lib/ is not system
        self.assertFalse(is_system_lib("/nix/store/hash-x/usr/lib/libfoo.dylib"))

    def test_nix_store_hash(self):
        p = Path(
            "/nix/store/1234567890abcdef-gstreamer-1.0/lib/gstreamer-1.0/libgst.so"
        )
        self.assertEqual(nix_store_hash(p), "1234567890abcdef")
        self.assertIsNone(nix_store_hash(Path("/usr/local/lib/libfoo.dylib")))


class TestTargetNamer(unittest.TestCase):
    def test_standard(self):
        namer = TargetNamer()
        self.assertEqual(
            namer.target(Path("/nix/store/hash1-pkg1/lib/libfoo.dylib")).filename,
            "hash1-libfoo.dylib",
        )

    def test_collision(self):
        namer = TargetNamer()
        a = namer.target(Path("/nix/store/hashA-pkg/lib/libfoo.dylib"))
        b = namer.target(Path("/nix/store/hashA-pkg/lib/subdir/libfoo.dylib"))
        self.assertEqual(a.filename, "hashA-libfoo.dylib")
        self.assertEqual(b.filename, "hashA-subdir-libfoo.dylib")

    def test_reserved_names_are_never_handed_out(self):
        namer = TargetNamer(reserved=["libfoo.dylib"])
        t = namer.target(Path("/opt/x/lib/libfoo.dylib"))
        self.assertNotEqual(t.filename, "libfoo.dylib")


class BundleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.out = self.root / "out"
        self.out.mkdir()
        self.store = self.root / "store"
        self.tool = FakeToolchain()

    def tearDown(self):
        self._tmp.cleanup()

    def bundle(self, pkgs=(), seed_glob="*", tool=None):
        cfg = AppConfig(
            out_dir=self.out,
            pkgs_paths=[Path(p) for p in pkgs],
            quiet=True,
            scan_all=False,
            seed_glob=seed_glob,
        )
        b = DylibBundler(cfg, tool or self.tool)
        b.run()
        return b

    def deps(self, f: Path):
        return list(self.tool.inspect(f).deps)

    def real_files(self):
        return sorted(
            p.name for p in self.out.iterdir() if p.is_file() and not p.is_symlink()
        )


class TestConsumers(BundleCase):
    def test_outpath_consumer_is_relinked(self):
        """Regression: Wine's winegstreamer.so kept absolute store refs and still 'passed'."""
        libglib = write_fake(self.store / "glib/lib/libglib-2.0.0.dylib")
        consumer = write_fake(
            self.out / "winegstreamer.so",
            deps=["@rpath/ntdll.so", str(libglib)],
            rpaths=["@loader_path/", str(libglib.parent)],
            install_name="@rpath/winegstreamer.so",
        )
        write_fake(self.out / "ntdll.so", install_name="@rpath/ntdll.so")

        self.bundle()

        self.assertEqual(
            self.deps(consumer), ["@rpath/ntdll.so", "@loader_path/libglib-2.0.0.dylib"]
        )
        m = self.tool.inspect(consumer)
        self.assertEqual(m.rpaths, ("@loader_path/",))  # store rpath dropped
        self.assertEqual(m.install_name, "@rpath/winegstreamer.so")  # own id untouched
        self.assertIn("libglib-2.0.0.dylib", self.real_files())

    def test_unrelinked_consumer_fails_verification(self):
        libglib = write_fake(self.store / "glib/lib/libglib-2.0.0.dylib")
        write_fake(
            self.out / "winegstreamer.so", deps=[str(libglib)], filetype=MH_BUNDLE
        )
        with self.assertRaises(typer.Exit):
            self.bundle(seed_glob="")  # consumer deliberately not seeded

    def test_seed_glob_restricts_consumers(self):
        lib = write_fake(self.store / "a/lib/liba.dylib")
        keep = write_fake(self.out / "keep.so", deps=[str(lib)], filetype=MH_BUNDLE)
        with self.assertRaises(typer.Exit):  # skip.so still links the store
            write_fake(self.out / "skip.dylib", deps=[str(lib)])
            self.bundle(seed_glob="*.so")
        self.assertEqual(self.deps(keep), ["@loader_path/liba.dylib"])


class TestGraph(BundleCase):
    def test_transitive_deps_and_install_names(self):
        libc = write_fake(self.store / "c/lib/libc_.dylib")
        libb = write_fake(self.store / "b/lib/libb.dylib", deps=[str(libc)])
        liba = write_fake(self.store / "a/lib/liba.dylib", deps=[str(libb)])

        self.bundle(pkgs=[self.store / "a"])

        self.assertEqual(self.real_files(), ["liba.dylib", "libb.dylib", "libc_.dylib"])
        self.assertEqual(
            self.deps(self.out / "liba.dylib"), ["@loader_path/libb.dylib"]
        )
        self.assertEqual(
            self.deps(self.out / "libb.dylib"), ["@loader_path/libc_.dylib"]
        )
        self.assertEqual(
            self.tool.inspect(self.out / "libc_.dylib").install_name,
            "@loader_path/libc_.dylib",
        )
        # sources are never touched
        self.assertEqual(self.deps(liba), [str(libb)])

    def test_bundle_plugins_get_no_id(self):
        plugin = write_fake(
            self.store / "gst/lib/gstreamer-1.0/libgstfoo.so", filetype=MH_BUNDLE
        )
        self.bundle(pkgs=[self.store / "gst"])
        rw = [r for r in self.tool.rewrites if r["binary"].name == plugin.name]
        self.assertTrue(all(r["new_id"] is None for r in rw))

    def test_symlink_chain_becomes_aliases(self):
        real = write_fake(self.store / "v/lib/libvulkan.1.4.341.dylib")
        (self.store / "v/lib/libvulkan.1.dylib").symlink_to("libvulkan.1.4.341.dylib")
        (self.store / "v/lib/libvulkan.dylib").symlink_to("libvulkan.1.dylib")
        consumer = write_fake(
            self.out / "winevulkan.so",
            deps=[str(self.store / "v/lib/libvulkan.1.dylib")],
            filetype=MH_BUNDLE,
        )

        self.bundle(pkgs=[self.store / "v"])

        self.assertEqual(
            self.real_files(), ["libvulkan.1.4.341.dylib", "winevulkan.so"]
        )
        self.assertEqual(self.deps(consumer), ["@loader_path/libvulkan.1.4.341.dylib"])
        for alias in ("libvulkan.1.dylib", "libvulkan.dylib"):
            self.assertEqual(os.readlink(self.out / alias), real.name)

    def test_rpath_and_loader_path_references_are_followed(self):
        libz = write_fake(self.store / "z/lib/libz.dylib")
        sib = write_fake(self.store / "a/lib/libsib.dylib")
        liba = write_fake(
            self.store / "a/lib/liba.dylib",
            deps=["@rpath/libz.dylib", "@loader_path/libsib.dylib"],
            rpaths=[str(libz.parent)],
        )
        consumer = write_fake(self.out / "c.so", deps=[str(liba)], filetype=MH_BUNDLE)

        self.bundle()  # nothing but the consumer as a root

        self.assertEqual(
            self.real_files(), ["c.so", "liba.dylib", "libsib.dylib", "libz.dylib"]
        )
        self.assertEqual(
            self.deps(self.out / "liba.dylib"),
            ["@loader_path/libz.dylib", "@loader_path/libsib.dylib"],
        )
        self.assertEqual(self.tool.inspect(self.out / "liba.dylib").rpaths, ())
        del consumer, sib

    def test_dlopen_only_library_behind_rpath_is_bundled(self):
        """sdl2-compat dlopen()s libSDL3.dylib through its LC_RPATH; no load command."""
        sdl3 = write_fake(self.store / "sdl3/lib/libSDL3.0.dylib")
        (self.store / "sdl3/lib/libSDL3.dylib").symlink_to(sdl3.name)
        write_fake(
            self.store / "sdl2/lib/libSDL2-2.0.0.dylib", rpaths=[str(sdl3.parent)]
        )

        self.bundle(pkgs=[self.store / "sdl2"])

        self.assertIn("libSDL3.0.dylib", self.real_files())
        self.assertEqual(os.readlink(self.out / "libSDL3.dylib"), "libSDL3.0.dylib")

    def test_missing_weak_dependency_is_tolerated(self):
        write_fake(
            self.out / "c.so",
            deps=[str(self.store / "gone/lib/libgone.dylib")],
            weak=[str(self.store / "gone/lib/libgone.dylib")],
            filetype=MH_BUNDLE,
        )
        self.bundle()  # weak + absent: warning only

    def test_same_basename_from_two_packages(self):
        a = write_fake(self.store / "iconv-a/lib/libiconv.2.dylib")
        b = write_fake(self.store / "iconv-b/lib/libiconv.2.dylib")
        write_fake(self.out / "first.so", deps=[str(a)], filetype=MH_BUNDLE)
        write_fake(self.out / "second.so", deps=[str(b)], filetype=MH_BUNDLE)

        self.bundle()

        copies = [n for n in self.real_files() if n.endswith("libiconv.2.dylib")]
        self.assertEqual(len(copies), 2)
        targets = {self.deps(self.out / n)[0] for n in ("first.so", "second.so")}
        self.assertEqual(len(targets), 2)  # each consumer keeps its own copy

    def test_rerun_is_idempotent(self):
        lib = write_fake(self.store / "a/lib/liba.1.dylib")
        (self.store / "a/lib/liba.dylib").symlink_to(lib.name)
        write_fake(self.out / "c.so", deps=[str(lib)], filetype=MH_BUNDLE)

        self.bundle(pkgs=[self.store / "a"])
        first = sorted(os.listdir(self.out))
        self.bundle(pkgs=[self.store / "a"])
        self.assertEqual(sorted(os.listdir(self.out)), first)


class TestVerify(BundleCase):
    def test_reports_everything_wrong_on_disk(self):
        write_fake(self.out / "abs.so", deps=["/nix/store/x-foo/lib/libfoo.dylib"])
        write_fake(self.out / "miss.so", deps=["@loader_path/libnothere.dylib"])
        write_fake(self.out / "rp.so", rpaths=["/nix/store/x-foo/lib"])
        write_fake(self.out / "rel.so", deps=["libbare.dylib"])
        (self.out / "dangling.dylib").symlink_to("nowhere.dylib")
        write_fake(
            self.out / "ok.so", deps=["/usr/lib/libSystem.B.dylib"], filetype=MH_EXECUTE
        )
        (self.out / "README").write_text("not a binary")

        report = verify_bundle(self.out, self.tool)
        joined = "\n".join(report.errors)
        for needle in ("abs.so", "miss.so", "rp.so", "rel.so", "dangling.dylib"):
            self.assertIn(needle, joined)
        self.assertNotIn("ok.so", joined)
        self.assertEqual(len(report.errors), 5)


class TestCli(BundleCase):
    def test_missing_outpath_is_an_error(self):
        self.out.rmdir()
        with self.assertRaises(typer.Exit):
            self.bundle()


@unittest.skipUnless(
    sys.platform == "darwin"
    and all(shutil.which(t) for t in ("clang", "install_name_tool")),
    "needs clang and install_name_tool on macOS",
)
class TestRealMachO(BundleCase):
    """End to end with real dylibs: build, bundle, move, dlopen."""

    def cc(self, out: Path, src: str, *args: str):
        c = out.with_suffix(".c")
        c.parent.mkdir(parents=True, exist_ok=True)
        c.write_text(src)
        subprocess.run(
            [
                "clang",
                "-Wl,-headerpad_max_install_names",
                *args,
                "-o",
                str(out),
                str(c),
            ],
            check=True,
            capture_output=True,
        )
        return out

    def test_bundle_moves_and_loads(self):
        libb = self.cc(
            self.store / "b/lib/libb.1.dylib",
            "int b(void){return 41;}",
            "-dynamiclib",
            f"-Wl,-install_name,{self.store}/b/lib/libb.1.dylib",
        )
        (libb.parent / "libb.dylib").symlink_to(libb.name)
        liba = self.cc(
            self.store / "a/lib/liba.dylib",
            "int b(void); int a(void){return b()+1;}",
            "-dynamiclib",
            "-Wl,-install_name,@rpath/liba.dylib",
            f"-L{libb.parent}",
            "-lb",
        )
        self.cc(
            self.out / "consumer.so",
            "int a(void); int consumer(void){return a();}",
            "-bundle",
            f"-L{liba.parent}",
            "-la",
            f"-Wl,-rpath,{liba.parent}",
        )

        self.bundle(tool=MacToolchain())

        moved = self.root / "moved"
        shutil.copytree(self.out, moved, symlinks=True)
        shutil.rmtree(self.store)  # nothing may still point at the originals
        probe = self.cc(
            self.root / "probe",
            "#include <dlfcn.h>\n#include <stdio.h>\n"
            "int main(int c,char**v){void*h=dlopen(v[1],RTLD_NOW);"
            "if(!h){puts(dlerror());return 2;}"
            'int(*f)(void)=(int(*)(void))dlsym(h,"consumer");return f()==42?0:1;}',
        )
        res = subprocess.run(
            [str(probe), str(moved / "consumer.so")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(verify_bundle(moved, MacToolchain()).errors, [])


if __name__ == "__main__":
    unittest.main()

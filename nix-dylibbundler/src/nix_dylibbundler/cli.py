# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""
macOS Native Dylib Bundler

Recursively discovers, copies and relinks macOS dynamic libraries into a single flat
directory so the result is relocatable.

Pipeline:
  1. discover  BFS over the dependency graph. Roots are the Mach-O files already in
               the outpath (the consumers, e.g. Wine's *.so) and every library under
               the given package roots. Load commands are read straight from the
               Mach-O headers; @rpath and @loader_path references are resolved the
               way dyld would, so nothing is skipped just because it is not absolute.
  2. place     every library outside the outpath is copied in as
               `{nix-hash}-{basename}` (basename of the *real* file, after symlinks),
               and every name it was reached through gets a plain `{name}` symlink so
               dlopen("libvulkan.1.dylib") style lookups keep working.
  3. relink    every edge whose target moved is rewritten to `@loader_path/...`,
               copied dylibs get `@loader_path/{name}` as install name, and absolute
               non-system LC_RPATH entries (/nix/store/...) are deleted.
  4. verify    independent of the bookkeeping above: every Mach-O file found on disk
               in the outpath is re-read and each of its references must resolve
               inside the bundle or to the host OS. This is what catches a consumer
               that was never relinked.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Protocol

import typer

from .macho import MachO, read_macho

# .dylib, .so, versioned shared objects (.so.1, .so.1.2) and libfoo.1.dylib
LIB_SUFFIX_RE = re.compile(r"\.(?:dylib|so)(?:\.\d+)*$")

# Absolute prefixes that always belong to the host OS (SIP protected, never bundled).
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")

# Basenames provided by the OS runtime; bundling them causes duplicate-runtime breakage.
SYSTEM_NAME_MARKERS = ("libSystem", "libc++", "libobjc")

# Nix's stub packages for Apple frameworks.
SYSTEM_SUBSTRINGS = ("-apple-framework-",)

STORE_HASH_RE = re.compile(r"^[0-9a-z]{32}-")


# --------------------------------------------------------------------------- helpers


def is_dynamic_library(path: Path) -> bool:
    """Extension test only; callers must additionally check the file itself."""
    return bool(LIB_SUFFIX_RE.search(path.name))


def is_system_lib(dep: str) -> bool:
    """True for libraries expected to be provided by the host OS."""
    if any(marker in dep for marker in SYSTEM_SUBSTRINGS):
        return True
    if dep.startswith(SYSTEM_PREFIXES):
        return True
    return dep.rsplit("/", 1)[-1].startswith(SYSTEM_NAME_MARKERS)


def is_host_path(dep: str) -> bool:
    """An absolute path that exists on every target Mac (unlike a store copy of libc++)."""
    return dep.startswith(SYSTEM_PREFIXES)


def nix_store_hash(path: Path) -> str | None:
    """Hash of the store component containing `path`, regardless of nesting depth."""
    parts = path.parts
    if len(parts) >= 4 and parts[0] == os.sep and parts[1:3] == ("nix", "store"):
        return parts[3].split("-", 1)[0]
    return None


def symlink_chain(path: Path) -> list[Path]:
    """`path` followed by every link it points through (loop safe)."""
    chain = [path]
    seen: set[Path] = set()
    cur = path
    while cur.is_symlink() and cur not in seen:
        seen.add(cur)
        try:
            link = Path(os.readlink(cur))
        except OSError:
            break
        cur = link if link.is_absolute() else cur.parent / link
        chain.append(cur)
    return chain


def make_writable(path: Path) -> None:
    """Store copies arrive read-only; install_name_tool needs write access."""
    path.chmod(path.stat().st_mode | stat.S_IWUSR)


def loader_ref(from_file: Path, to_file: Path) -> str:
    return "@loader_path/" + os.path.relpath(to_file, from_file.parent)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


# ------------------------------------------------------------------------- toolchain


class BinaryToolchain(Protocol):
    """Strategy interface for binary inspection/manipulation (swappable for tests)."""

    def inspect(self, binary: Path) -> MachO | None: ...

    def rewrite(
        self,
        binary: Path,
        changes: Iterable[tuple[str, str]],
        new_id: str | None = None,
        delete_rpaths: Iterable[str] = (),
    ) -> list[str]: ...


class MacToolchain:
    """Headers are parsed in Python; `install_name_tool` does the rewriting."""

    def __init__(self, install_name_tool: str = "install_name_tool"):
        self.install_name_tool = install_name_tool

    def inspect(self, binary: Path) -> MachO | None:
        return read_macho(binary)

    def rewrite(
        self,
        binary: Path,
        changes: Iterable[tuple[str, str]],
        new_id: str | None = None,
        delete_rpaths: Iterable[str] = (),
    ) -> list[str]:
        """Apply everything in one exec; on failure retry op by op.

        Returns non-fatal warnings instead of raising: one failing op must not abort
        the whole bundle, and the verify phase decides whether the result is usable.
        """
        ops: list[list[str]] = [["-change", old, new] for old, new in changes]
        ops += [["-delete_rpath", r] for r in delete_rpaths]
        if new_id:
            ops.append(["-id", new_id])
        if not ops:
            return []
        if self._try([a for op in ops for a in op], binary):
            return []
        return [
            f"{binary.name}: install_name_tool {' '.join(op)} failed"
            for op in ops
            if not self._try(op, binary)
        ]

    def _try(self, args: list[str], binary: Path) -> bool:
        try:
            subprocess.run(
                [self.install_name_tool, *args, str(binary)],
                check=True,
                capture_output=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


# ----------------------------------------------------------------------------- naming


@dataclass(frozen=True)
class Target:
    filename: str  # collision-isolated on-disk name inside the bundle
    pure: str  # original basename, used for the dlopen() fallback symlink


class TargetNamer:
    """Allocates one unique bundle filename per source path.

    Store libraries become `{hash}-{basename}`. If that is already claimed by a
    *different* file (two same-named libraries in different subdirectories of one
    store path), parent directory names are folded in, with a short digest as a
    guaranteed-unique last resort.
    """

    def __init__(self, reserved: Iterable[str] = ()):
        self._claimed: dict[str, Path | None] = {n: None for n in reserved}
        self._cache: dict[Path, Target] = {}

    def target(self, path: Path) -> Target:
        if path in self._cache:
            return self._cache[path]
        pure = path.name
        nix_hash = nix_store_hash(path)
        candidate = pure
        for extra in self._disambiguators(path):
            candidate = "-".join(p for p in (nix_hash, extra, pure) if p)
            if candidate not in self._claimed or self._claimed[candidate] == path:
                break
        result = Target(candidate, pure)
        self._claimed[candidate] = path
        self._cache[path] = result
        return result

    @staticmethod
    def _disambiguators(path: Path) -> Iterator[str]:
        yield ""  # {hash}-{name}
        parents = path.parent.parts
        for depth in (1, 2, 3):  # {hash}-gstreamer-1.0-{name}, ...
            if len(parents) >= depth:
                yield "-".join(parents[-depth:])
        yield hashlib.sha1(str(path).encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------- bundler


@dataclass(frozen=True)
class AppConfig:
    out_dir: Path
    pkgs_paths: list[Path]
    quiet: bool
    scan_all: bool
    seed_glob: str | None  # None/"" -> outpath is not scanned for consumers


@dataclass
class Node:
    src: Path  # real (symlink-free) path of the original file; graph identity
    macho: MachO
    in_place: bool  # already inside the outpath: relinked where it is, never copied
    inherited_rpaths: tuple[tuple[str, Path], ...]  # (rpath, loader dir) of loaders
    aliases: list[str] = field(default_factory=list)  # names it was reached through
    edges: list[tuple[str, Path]] = field(default_factory=list)  # (raw ref, dep src)
    location: Path | None = None  # where it lives in the bundle


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DylibBundler:
    def __init__(self, config: AppConfig, toolchain: BinaryToolchain):
        self.config = config
        self.toolchain = toolchain
        self.nodes: dict[Path, Node] = {}  # insertion order == discovery order
        self.queue: deque[Node] = deque()
        self.warnings: list[str] = []

    # ---------------------------------------------------------------- logging

    def _log(self, msg: str, **kwargs) -> None:
        if not self.config.quiet:
            typer.secho(msg, **kwargs)

    def _warn(self, msg: str) -> None:
        self.warnings.append(msg)
        typer.secho(f"  [!] {msg}", fg=typer.colors.YELLOW, err=True)

    def _error(self, msg: str) -> None:
        typer.secho(f"  [!] CRITICAL: {msg}", fg=typer.colors.RED, err=True)

    @staticmethod
    def _fail(msg: str) -> None:
        typer.secho(f"Error: {msg}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # -------------------------------------------------------------------- run

    def run(self) -> None:
        self._log("Starting Bundle Process...", fg=typer.colors.CYAN, bold=True)
        out = self.config.out_dir
        # Refuse to invent the directory: creating it would make an empty bundle
        # "succeed".
        if out.is_file():
            self._fail(f"Directory expected, yet '{out}' is a file.")
        if not out.is_dir():
            self._fail(f"Please create directory '{out}' before running the script.")

        self.discover()
        self.place()
        self.relink()
        report = self.verify()

        for w in report.warnings:
            self._warn(w)
        for e in report.errors:
            self._error(e)
        if report.errors:
            self._fail(
                f"validation failed: {len(report.errors)} unresolved reference(s)."
            )
        self._log(
            "Validation passed! Every Mach-O file in the outpath is relocatable.",
            fg=typer.colors.GREEN,
            bold=True,
        )

    # -------------------------------------------------------------- 1. discover

    def _iter_package_libs(self, root: Path) -> Iterator[Path]:
        """Every library under `<pkg>/lib` (or the whole root with --scan-all).

        Symlinks are yielded too, so their names become dlopen() aliases. `rglob` does
        not descend into directory symlinks, which avoids store loops.
        """
        base = root / "lib"
        if self.config.scan_all or not base.is_dir():
            base = root
        for f in sorted(base.rglob("*")):
            if is_dynamic_library(f) and f.is_file():
                yield f

    def _iter_consumers(self) -> Iterator[Path]:
        if not self.config.seed_glob:
            return
        for f in sorted(self.config.out_dir.glob(self.config.seed_glob)):
            # Symlinks are aliases created by an earlier run; the real file is matched
            # on its own.
            if f.is_file() and not f.is_symlink():
                yield f

    def _add(
        self,
        path: Path,
        inherited: tuple[tuple[str, Path], ...] = (),
        alias: bool = True,
    ) -> Node | None:
        chain = symlink_chain(path)
        src = path.resolve()
        node = self.nodes.get(src)
        if node is None:
            macho = self.toolchain.inspect(src)
            if macho is None:
                return None
            node = Node(
                src=src,
                macho=macho,
                in_place=_within(src, self.config.out_dir),
                inherited_rpaths=inherited,
            )
            self.nodes[src] = node
            self.queue.append(node)
        if alias:
            for p in chain:
                if p.name not in node.aliases:
                    node.aliases.append(p.name)
        return node

    def _rpath_dirs(self, node: Node) -> Iterator[Path]:
        own = tuple((r, node.src.parent) for r in node.macho.rpaths)
        for rpath, loader_dir in own + node.inherited_rpaths:
            if rpath.startswith("@loader_path"):
                yield loader_dir / rpath.removeprefix("@loader_path").lstrip("/")
            elif rpath.startswith("/"):
                yield Path(rpath)
            # @executable_path: the executable is not known here

    def resolve(self, node: Node, raw: str) -> Path | None:
        """Where dyld would find `raw` when loaded by `node` on the build machine."""
        if raw.startswith("@rpath/"):
            rest = raw.removeprefix("@rpath/")
            for d in self._rpath_dirs(node):
                if os.path.lexists(d / rest):
                    return d / rest
            return None
        if raw.startswith("@loader_path/"):
            return node.src.parent / raw.removeprefix("@loader_path/")
        if raw.startswith("@executable_path/"):
            return None
        return Path(raw) if raw.startswith("/") else None

    def discover(self) -> None:
        self._log("\nPhase 1: BFS Discovery", fg=typer.colors.BLUE, bold=True)

        consumers = 0
        for f in self._iter_consumers():
            if self._add(f, alias=False):
                consumers += 1
        if self.config.seed_glob and consumers == 0:
            self._warn(
                f"--seed-glob '{self.config.seed_glob}' matched no Mach-O files "
                f"under '{self.config.out_dir}'"
            )

        for pkg in self.config.pkgs_paths:
            for f in self._iter_package_libs(pkg):
                self._add(f)

        rpath_dirs_seen: set[Path] = set()
        from_rpaths = 0
        while self.queue:
            node = self.queue.popleft()
            # rpaths a dependency inherits from this loader (dyld search semantics)
            inherited = (
                tuple((r, node.src.parent) for r in node.macho.rpaths)
                + node.inherited_rpaths
            )
            # An absolute rpath is also a dlopen() search path: sdl2-compat finds SDL3
            # only through dlopen("libSDL3.dylib") + LC_RPATH, with no load command at
            # all. relink() deletes these rpaths, so whatever they could have supplied
            # has to be in the bundle.
            for r in node.macho.rpaths:
                d = Path(r)
                if not r.startswith("/") or is_host_path(r) or d in rpath_dirs_seen:
                    continue
                rpath_dirs_seen.add(d)
                if _within(d.resolve(), self.config.out_dir) or not d.is_dir():
                    continue
                for f in sorted(d.iterdir()):
                    if is_dynamic_library(f) and f.is_file():
                        before = len(self.nodes)
                        self._add(f)
                        from_rpaths += len(self.nodes) - before
            for raw in node.macho.deps:
                if is_system_lib(raw):
                    continue
                dep_path = self.resolve(node, raw)
                if dep_path is None:
                    if not (node.in_place and raw.startswith("@")):
                        self._warn(f"{node.src.name}: cannot resolve '{raw}'")
                    continue
                if not os.path.exists(dep_path):
                    if raw not in node.macho.weak_deps:
                        self._warn(f"{node.src.name}: dependency missing: {raw}")
                    continue
                dep = self._add(dep_path, inherited)
                if dep is None:
                    self._warn(f"{node.src.name}: '{raw}' is not a Mach-O file")
                    continue
                node.edges.append((raw, dep.src))

        copied = sum(not n.in_place for n in self.nodes.values())
        self._log(
            f"  Discovered {len(self.nodes)} Mach-O files "
            f"({len(self.nodes) - copied} in outpath, {copied} to bundle, "
            f"{from_rpaths} only reachable through an LC_RPATH directory)."
        )

    # ----------------------------------------------------------------- 2. place

    def place(self) -> None:
        self._log(
            "\nPhase 2: Copy & Collision Isolation", fg=typer.colors.BLUE, bold=True
        )
        out = self.config.out_dir
        # Consumers' own names must never be overwritten by a copy. Copies from an
        # earlier run (`{hash}-name`, or carrying the `@loader_path/{name}` install name
        # this bundler sets) are not reserved: the same source maps onto them again.
        reserved = [
            n.src.name
            for n in self.nodes.values()
            if n.in_place
            and not STORE_HASH_RE.match(n.src.name)
            and n.macho.install_name != f"@loader_path/{n.src.name}"
        ]
        namer = TargetNamer(reserved=reserved)

        for node in self.nodes.values():
            if node.in_place:
                node.location = node.src
                continue
            dest = out / namer.target(node.src).filename
            node.location = dest
            if os.path.lexists(dest):
                if dest.is_symlink() or not dest.is_file():
                    self._fail(f"'{dest}' exists and is not a regular file")
                continue  # earlier run; relink() works from its current contents
            shutil.copy2(node.src, dest)
            make_writable(dest)

        # dlopen() fallbacks. Discovery order decides ownership of a contested name, so
        # whatever the consumers link directly wins over transitive duplicates.
        owner: dict[str, Path] = {}
        for node in self.nodes.values():
            if node.in_place:
                continue
            assert node.location is not None
            shadowed = []
            for name in node.aliases:
                if name == node.location.name:
                    continue
                if name in owner:
                    if owner[name] != node.src:
                        shadowed.append(name)
                    continue
                owner[name] = node.src
                link = out / name
                if link.is_symlink():
                    if os.readlink(link) != node.location.name:
                        link.unlink()  # stale alias from an earlier run
                elif os.path.lexists(link):
                    self._warn(f"alias {name}: a real file already has that name")
                    continue
                if not os.path.lexists(link):
                    os.symlink(node.location.name, link)
            if shadowed and not self.config.quiet:
                self._log(
                    f"  {node.location.name}: plain name(s) {', '.join(shadowed)} "
                    "already taken by an earlier library"
                )

    # ---------------------------------------------------------------- 3. relink

    def relink(self) -> None:
        self._log(
            "\nPhase 3: Relinking to @loader_path", fg=typer.colors.BLUE, bold=True
        )
        nodes = list(self.nodes.values())
        if self.config.quiet:
            for node in nodes:
                self._relink(node)
        else:
            with typer.progressbar(nodes, label="  Relinking") as progress:
                for node in progress:
                    self._relink(node)

    def _relink(self, node: Node) -> None:
        assert node.location is not None
        # Work from what is on disk now: on a re-run the copy is already relinked.
        current = self.toolchain.inspect(node.location)
        if current is None:
            self._warn(f"{node.location.name}: no longer a Mach-O file")
            return

        changes: list[tuple[str, str]] = []
        for raw, dep_src in node.edges:
            dep = self.nodes[dep_src]
            assert dep.location is not None
            if node.in_place and dep.in_place and raw.startswith("@"):
                continue  # neither side moved and the reference is not absolute
            new = loader_ref(node.location, dep.location)
            if raw != new and raw in current.deps:
                changes.append((raw, new))

        drop = [r for r in current.rpaths if r.startswith("/") and not is_host_path(r)]

        new_id = None
        if not node.in_place and current.is_dylib:
            wanted = f"@loader_path/{node.location.name}"
            if current.install_name != wanted:
                new_id = wanted

        for w in self.toolchain.rewrite(node.location, changes, new_id, drop):
            self._warn(w)

    # ---------------------------------------------------------------- 4. verify

    def verify(self) -> Report:
        """Check the bundle as found on disk, trusting none of the state above.

        Every regular Mach-O file under the outpath is inspected, whether this run
        copied it, it was there already, or --seed-glob left it out.
        """
        self._log("\nPhase 4: Verification", fg=typer.colors.BLUE, bold=True)
        return verify_bundle(self.config.out_dir, self.toolchain)


def verify_bundle(out_dir: Path, toolchain: BinaryToolchain) -> Report:
    report = Report()
    by_pure_name: dict[str, list[str]] = {}

    for f in sorted(out_dir.rglob("*")):
        rel = f.relative_to(out_dir)
        if f.is_symlink():
            if not f.exists():
                report.errors.append(f"{rel}: broken symlink -> {os.readlink(f)}")
            elif not _within(f.resolve(), out_dir.resolve()):
                report.errors.append(
                    f"{rel}: symlink leaves the bundle -> {f.resolve()}"
                )
            continue
        if not f.is_file():
            continue
        m = toolchain.inspect(f)
        if m is None:
            continue
        by_pure_name.setdefault(STORE_HASH_RE.sub("", f.name), []).append(f.name)

        for r in m.rpaths:
            if r.startswith("/") and not is_host_path(r):
                report.errors.append(f"{rel}: absolute LC_RPATH {r}")

        for dep in m.deps:
            weak = dep in m.weak_deps
            problem: str | None = None
            if dep.startswith("/"):
                if not is_host_path(dep):
                    problem = f"links non-relocatable {dep}"
            elif dep.startswith("@loader_path/"):
                if not (f.parent / dep.removeprefix("@loader_path/")).exists():
                    problem = f"missing {dep}"
            elif dep.startswith("@rpath/"):
                rest = dep.removeprefix("@rpath/")
                dirs = [
                    f.parent / r.removeprefix("@loader_path").lstrip("/")
                    for r in m.rpaths
                    if r.startswith("@loader_path")
                ]
                if not any((d / rest).exists() for d in dirs):
                    # It may still resolve through the loading executable's rpaths.
                    report.warnings.append(
                        f"{rel}: {dep} does not resolve through its own rpaths"
                    )
            elif dep.startswith("@executable_path/"):
                report.warnings.append(f"{rel}: cannot verify {dep}")
            else:
                problem = f"relative install name {dep}"
            if problem:
                (report.warnings if weak else report.errors).append(
                    f"{rel}: {problem}" + (" (weak)" if weak else "")
                )

    for pure, names in by_pure_name.items():
        if len(names) > 1:
            report.warnings.append(
                f"{len(names)} different copies of {pure}: {', '.join(names)}"
            )
    return report


# -------------------------------------------------------------------------------- CLI

app = typer.Typer(add_completion=False, help="macOS Native Dylib Bundler")


@app.command()
def bundle(
    outpath: Annotated[
        Path,
        typer.Option("--outpath", "-o", help="Target directory for bundled libraries"),
    ],
    pkgs: Annotated[
        list[Path],
        # No default inside Annotated: Typer rejects that. Absent default == required.
        typer.Option("--pkgs", "-p", help="Package roots to traverse (repeatable)"),
    ],
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress verbose logging output")
    ] = False,
    scan_all: Annotated[
        bool,
        typer.Option(
            "--scan-all", help="Scan the whole package root instead of just <pkg>/lib"
        ),
    ] = False,
    otool: Annotated[
        str | None,
        typer.Option("--otool", hidden=True, help="Unused; kept for compatibility"),
    ] = None,
    install_name_tool: Annotated[
        str,
        typer.Option(
            "--install-name-tool",
            hidden=True,
            help="Path to install_name_tool executable",
        ),
    ] = "install_name_tool",
    seed_glob: Annotated[
        str,
        typer.Option(
            "--seed-glob",
            help="Glob (relative to --outpath) selecting the Mach-O files already in "
            "the outpath whose Nix references get relinked. Pass '' to relink "
            "nothing that was already there.",
        ),
    ] = "*",
):
    """Bundle external runtime dependencies natively using BFS."""
    # Fail fast on bad package roots. A common cause is an empty $(...) command
    # substitution (e.g. a failed `nix build`), which otherwise shifts argv.
    bad_pkgs = [p for p in pkgs if not p.exists()]
    if bad_pkgs:
        for p in bad_pkgs:
            typer.secho(
                f"Error: --pkgs path does not exist: '{p}'",
                fg=typer.colors.RED,
                err=True,
            )
        raise typer.Exit(code=1)

    config = AppConfig(
        out_dir=outpath.resolve(),
        pkgs_paths=[p.resolve() for p in pkgs],
        quiet=quiet,
        scan_all=scan_all,
        seed_glob=seed_glob,
    )
    DylibBundler(config, MacToolchain(install_name_tool=install_name_tool)).run()


if __name__ == "__main__":
    app()

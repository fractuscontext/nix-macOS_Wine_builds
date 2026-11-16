#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""Decide what CI does for this commit, then do it.

| Event                         | Flavours             | Does      | Releases   |
|-------------------------------|----------------------|-----------|------------|
| push to main with a change    | changed              | full build| yes        |
| any other push, pull request  | changed, else latest | configure | no         |
| /build comment on a PR        | changed, else latest | full build| no         |
| manual dispatch               | CI_FLAVOUR           | full build| CI_RELEASE |

A flavour counts as changed when its source pin moved, or when a file that only
changes its build (patches/, the DXMT binary patch) differs from CI_BASE.

Every full build ends with a WineTest smoke run (CI_WINETESTS, both arches). A
test failing inside it is expected and only logged; Wine not producing a report
at all fails that flavour.

Inputs (environment, so it runs the same locally and on GitHub):
  CI_EVENT    push | pull_request | issue_comment | workflow_dispatch
  CI_REF      git ref being built; refs/heads/main allows auto-release
  CI_BASE     commit to diff pins against; empty or all-zero means "no bump"
  CI_FLAVOUR  workflow_dispatch only
  CI_RELEASE  workflow_dispatch only, "true" to release
  CI_WINETESTS  WineTest smoke tests, space separated; "" skips them
  CI_DRY_RUN  "1" prints the plan and stops

Outputs in output/: <tag>.zip and <tag>-winetest{32,64}.report per full build,
releases.txt with the tags to release, and `built` / `to_release` in $GITHUB_OUTPUT when set.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PINS = Path("_sources/generated.json")
SOURCE = Path("sources/wine")
OUT = Path("output")
MAIN = "refs/heads/main"

# nvfetcher pin name -> `just clone` flavour
FLAVOURS = {"crossover-source": "crossover", "wine-latest": "latest"}
DEFAULT_FLAVOUR = "latest"

# Files that change a flavour's build without moving its pin
REBUILD_PATHS = {
    "latest": ("patches/wine-latest/", "nix/dxmt-cross-process.py"),
    "crossover": ("nix/dxmt-cross-process.py",),
}
DEFAULT_WINETESTS = "version mscoree"


@dataclass(frozen=True)
class Plan:
    flavours: tuple[str, ...]
    build: bool
    release: bool


# --- pure core ---------------------------------------------------------------


def versions(pins_json: str) -> dict[str, str]:
    pins = json.loads(pins_json)
    return {FLAVOURS[name]: pin["version"] for name, pin in pins.items() if name in FLAVOURS}


def bumped(old: dict[str, str], new: dict[str, str]) -> tuple[str, ...]:
    return tuple(flavour for flavour, version in new.items() if old.get(flavour) != version)


def touched(paths: list[str]) -> tuple[str, ...]:
    """Flavours whose build inputs (outside the pins) appear in `paths`."""
    return tuple(
        flavour
        for flavour, prefixes in REBUILD_PATHS.items()
        if any(path.startswith(prefixes) for path in paths)
    )


def merge(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(flavour for group in groups for flavour in group))


def decide(event: str, ref: str, bumps: tuple[str, ...], flavour: str, release: bool) -> Plan:
    fallback = bumps or (DEFAULT_FLAVOUR,)
    match event:
        case "push" if ref == MAIN and bumps:
            return Plan(bumps, build=True, release=True)
        case "push" | "pull_request":
            return Plan(fallback, build=False, release=False)
        case "issue_comment":
            return Plan(fallback, build=True, release=False)
        case "workflow_dispatch":
            return Plan((flavour,), build=True, release=release)
        case _:
            raise SystemExit(f"Unknown CI_EVENT: {event!r}")


# --- effects -----------------------------------------------------------------


def run(*cmd: str | Path) -> None:
    print("+", *cmd, flush=True)
    subprocess.run(cmd, check=True)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, capture_output=True, text=True)


def pins_at(base: str, current: dict[str, str]) -> dict[str, str]:
    if not base.strip("0"):
        return current  # no base (manual run, new branch): nothing counts as bumped
    if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        raise SystemExit(f"Base commit {base} is not in this checkout (fetch more history)")
    shown = git("show", f"{base}:{PINS}")
    return versions(shown.stdout) if shown.returncode == 0 else {}


def changed_paths(base: str) -> list[str]:
    if not base.strip("0"):
        return []
    diff = git("diff", "--name-only", base, "HEAD")
    if diff.returncode != 0:
        raise SystemExit(f"git diff {base} failed: {diff.stderr.strip()}")
    return diff.stdout.split()


def configure(flavour: str) -> None:
    run("just", "clone", flavour)
    run("just", "configure")


def build(flavour: str) -> str:
    run("just", "clone", flavour)
    tag = (SOURCE / "WINE_FLAVOUR").read_text().strip()
    prefix = (OUT / tag).absolute()
    run("just", f"out={prefix}", "all")
    run("ditto", "-c", "-k", "--keepParent", prefix, OUT / f"{tag}.zip")
    smoke_test(prefix, tag)
    return tag


def smoke_test(prefix: Path, tag: str) -> None:
    """Run a few WineTest tests against the fresh build, in a throwaway prefix."""
    tests = os.environ.get("CI_WINETESTS", DEFAULT_WINETESTS).split()
    if not tests:
        return
    env = {**os.environ, "WINEPREFIX": str((OUT / f"{tag}-winetest-prefix").absolute())}
    for arch in ("32", "64"):
        report = OUT / f"{tag}-winetest{arch}.report"
        cmd = ["just", f"out={prefix}", f"winetest{arch}", "-c", "-t", "ci", "-o", str(report.absolute()), *tests]
        print("+", *cmd, flush=True)
        # winetest exits non-zero when any test fails; only a missing report means Wine broke
        subprocess.run(cmd, env=env, check=False)
        if not report.is_file():
            raise subprocess.CalledProcessError(1, cmd, f"winetest{arch} produced no report")
        summary = [line for line in report.read_text(errors="replace").splitlines() if "tests executed" in line]
        print(f"[ci] winetest{arch}:", *summary, sep="\n  ", flush=True)
    subprocess.run([prefix / "usr/local/bin/wineserver", "-k"], env=env, check=False)


def attempt[T](step: Callable[[str], T], flavour: str) -> T | Literal[False]:
    """Run one flavour; a failure is reported but doesn't stop the others."""
    try:
        return step(flavour)
    except subprocess.CalledProcessError as err:
        print(f"[ci] {flavour} failed: {err}", flush=True)
        return False


def report(plan: Plan, tags: list[str]) -> None:
    to_release = plan.release and bool(tags)
    OUT.mkdir(exist_ok=True)
    (OUT / "releases.txt").write_text("".join(f"{tag}\n" for tag in tags) if to_release else "")

    if github_output := os.environ.get("GITHUB_OUTPUT"):
        with open(github_output, "a") as fh:
            fh.write(f"built={str(plan.build).lower()}\n")
            fh.write(f"to_release={str(to_release).lower()}\n")


def main() -> None:
    env = os.environ.get
    event = env("CI_EVENT", "push")

    base = env("CI_BASE", "")
    current = versions(PINS.read_text())
    bumps = merge(bumped(pins_at(base, current), current), touched(changed_paths(base)))
    plan = decide(
        event,
        ref=env("CI_REF", ""),
        bumps=bumps,
        flavour=env("CI_FLAVOUR", DEFAULT_FLAVOUR),
        release=env("CI_RELEASE") == "true",
    )
    print(f"[ci] {event}: changed={list(bumps)} -> {plan}", flush=True)
    if env("CI_DRY_RUN") == "1":
        return

    step = build if plan.build else configure
    results = {flavour: attempt(step, flavour) for flavour in plan.flavours}

    # Release whatever succeeded, then fail the job if anything didn't
    report(plan, [tag for tag in results.values() if isinstance(tag, str)])
    if failed := [flavour for flavour, result in results.items() if result is False]:
        raise SystemExit(f"[ci] failed: {failed}")


if __name__ == "__main__":
    main()

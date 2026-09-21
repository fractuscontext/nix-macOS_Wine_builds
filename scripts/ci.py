#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

"""Decide what CI does for this commit, then do it.

| Event                         | Flavours             | Does      | Releases   |
|-------------------------------|----------------------|-----------|------------|
| push to main with a pin bump  | bumped               | full build| yes        |
| any other push, pull request  | bumped, else latest  | configure | no         |
| /build comment on a PR        | bumped, else latest  | full build| no         |
| manual dispatch               | CI_FLAVOUR           | full build| CI_RELEASE |

Inputs (environment, so it runs the same locally and on GitHub):
  CI_EVENT    push | pull_request | issue_comment | workflow_dispatch
  CI_REF      git ref being built; refs/heads/main allows auto-release
  CI_BASE     commit to diff pins against; empty or all-zero means "no bump"
  CI_FLAVOUR  workflow_dispatch only
  CI_RELEASE  workflow_dispatch only, "true" to release
  CI_DRY_RUN  "1" prints the plan and stops

Outputs in output/: <tag>.zip per full build, releases.txt with the tags to
release, and `built` / `to_release` in $GITHUB_OUTPUT when set.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

PINS = Path("_sources/generated.json")
SOURCE = Path("sources/wine")
OUT = Path("output")
MAIN = "refs/heads/main"

# nvfetcher pin name -> `just clone` flavour
FLAVOURS = {"crossover-source": "crossover", "wine-latest": "latest"}
DEFAULT_FLAVOUR = "latest"


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


def release_tag(wine_flavour: str, now: datetime) -> str:
    return f"{wine_flavour}-{now:%y.%m%d.%H%M}"


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


def configure(flavour: str) -> None:
    run("just", "clone", flavour)
    run("just", "configure")


def build(flavour: str, now: datetime) -> str:
    run("just", "clone", flavour)
    tag = release_tag((SOURCE / "WINE_FLAVOUR").read_text().strip(), now)
    prefix = (OUT / tag).absolute()
    run("just", f"out={prefix}", "all")
    run("ditto", "-c", "-k", "--keepParent", prefix, OUT / f"{tag}.zip")
    return tag


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

    current = versions(PINS.read_text())
    bumps = bumped(pins_at(env("CI_BASE", ""), current), current)
    plan = decide(
        event,
        ref=env("CI_REF", ""),
        bumps=bumps,
        flavour=env("CI_FLAVOUR", DEFAULT_FLAVOUR),
        release=env("CI_RELEASE") == "true",
    )
    print(f"[ci] {event}: bumped={list(bumps)} -> {plan}", flush=True)
    if env("CI_DRY_RUN") == "1":
        return

    now = datetime.now(UTC)
    step = (lambda f: build(f, now)) if plan.build else configure
    results = {flavour: attempt(step, flavour) for flavour in plan.flavours}

    # Release whatever succeeded, then fail the job if anything didn't
    report(plan, [tag for tag in results.values() if isinstance(tag, str)])
    if failed := [flavour for flavour, result in results.items() if result is False]:
        raise SystemExit(f"[ci] failed: {failed}")


if __name__ == "__main__":
    main()

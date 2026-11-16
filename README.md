<!--
SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>

SPDX-License-Identifier: GPL-3.0-only
-->

<!-- markdownlint-disable MD013 MD033 MD041 -->
<div align="center">

  <h1>Compiling Wine 11 for macOS</h1>

  <p>Safer, reproducible Wine builds for macOS.</p>

  <p>
    <a href="https://github.com/fractuscontext/nix-wine-macos/actions/workflows/ci.yml"><img src="https://github.com/fractuscontext/nix-wine-macos/actions/workflows/ci.yml/badge.svg" alt="CI status" /></a>
    <a href="https://nixos.org/"><img src="https://img.shields.io/badge/Nix-flake-5277c3?logo=nixos&logoColor=white" alt="Nix flake" /></a>
    <img src="https://img.shields.io/badge/macOS-x86__64%20%E2%89%A5%2015-000000?logo=apple&logoColor=white" alt="macOS >= 15 | x86_64" />
    <a href="LICENSES/GPL-3.0-only.txt"><img src="https://img.shields.io/badge/License-GPL--3.0--only-blue" alt="GPL-3.0-only" /></a>
  </p>

</div>

---

## Why?

Similar to [Gcenx/macOS_Wine_builds](https://github.com/Gcenx/macOS_Wine_builds), but safer and more transparent.

Most community builds are uploaded by hand, so a compromised machine or a broken Xcode install turns into a supply-chain risk nobody can detect.

## What works

- [x] GnuTLS (Working)
- [x] MoltenVK / Vulkan (Working)
- [x] DXMT v0.80  (Working)
- [x] Wine Mono / .NET, bundled (Working)
- [x] Chromium / CEF with GPU compositing, e.g. `cefclient` (Working, `latest` fork only; see [Patches](#patches))
- [ ] OpenGL 4.2+ (Not Working)

Two source forks are available:

| Fork | Source | Release tag |
| --- | --- | --- |
| `latest` | The upstream WineHQ release tag, checked daily | `wine-11.22` |
| `crossover` | CodeWeavers' CrossOver sources | `cx-26.3.0` |

## Quick start

You need [Nix](https://nixos.org/download/) with flakes enabled on an Apple Silicon Mac. The build runs natively on aarch64 and cross-compiles x86_64 Wine with Clang.

```bash
git clone https://github.com/fractuscontext/nix-wine-macos.git
cd nix-wine-macos
nix develop
just clone latest   # or: just clone crossover
just all
```

Wine binary ends up in `output/wow64`. Run it with `./output/wow64/usr/local/bin/wine winecfg`.

## Manual Build

`just all` runs the steps below in order. Each one is also a recipe of its own, which helps when one step fails and you want to rerun only that step.

```bash
nix develop         # install deps & stuff
just clone latest   # copies source tree out of the Nix store into `sources/wine`

# Alternatively, use your own source:
# git clone --depth 1 --branch wine-11.20 https://gitlab.winehq.org/wine/wine.git sources/wine

just configure      # configures, please see ./justfile for flag list
just make           # calls make
just install        # calls make install, installs into `output/wow64/usr/local`.
just install-dxmt   # replaces with DXMT
just install-mono   # ships the Wine Mono matching the source in share/wine/mono
just bundle         # Copies dylib to make `output/wow64` is self-contained and portable.
```

```bash
./output/wow64/usr/local/bin/wine winecfg
```

Other commands:

| Command | What it does |
| --- | --- |
| `just shell` | Opens a shell with the exact build environment, for running compile commands by hand |
| `just pin <pin> <version>` | Sets a hand-pinned version (`crossover-source`, `wine-mono-crossover`) in `nvfetcher.toml`; follow with `just update-source <pin>` |
| `just update-source [pin]` | Updates the source pins with nvfetcher, either all of them or one (`wine-latest`, `crossover-source`, `wine-mono-latest`, `wine-mono-crossover`, `winetest`, `winetest64`) |
| `just winetest32 [args]` / `just winetest64 [args]` | Runs the pinned 32-/64-bit [WineTest](https://gitlab.winehq.org/wine/wine/-/wikis/Conformance-Tests) against the build, passing your arguments through |
| `just src=../wine out=/tmp/wow64 all` | Builds a different source tree into a different output directory |

Releases are tagged with the Wine version alone, e.g. `wine-11.22` or `cx-26.3.0`. Rebuilding the same version (say, after a patch change) replaces that release.

## CI

| Workflow | When | What it does |
| --- | --- | --- |
| CI | every push and PR | Configures. On `main`, a moved source pin or a change under `patches/wine-latest/` (or to the DXMT binary patch) does a full build and release of that fork |
| Update Wine (latest tag) | daily | Bumps `wine-latest` with its matching `wine-mono-latest`; on a new tag also refreshes the WineTest pins. Opens a PR |
| Update CrossOver | manual, takes a version | Pins CrossOver, reads its `MONO_VERSION` and pins `wine-mono-crossover` to match. Opens a PR |
| Manual Build | manual | Full build of one fork, optionally released |
| PR Build | `/build` comment on a PR | Full build of the PR, never released |

Every full build ends with a WineTest smoke run (`version mscoree`, 32- and 64-bit; override with `CI_WINETESTS`). Failing tests are only logged, since some always fail (e.g. locale ones); Wine not producing a report at all fails the build. Reports are uploaded as a `winetest-…` artifact, or inside the PR build artifact.

## Patches

The `latest` fork applies everything in [`patches/wine-latest/`](patches/wine-latest/) when the source is pinned (`nix/sources.nix`), so `just clone latest` always gives a patched tree:

| Patch | What it does |
| --- | --- |
| `0001-winemac-export-macdrv_functions-for-DXMT` | Exports the `macdrv_functions` table DXMT presents through. Without it, stock Wine gives DXMT no way to draw into a window. |
| `0002-winemac-cross-process-child-window-metal` | Lets a GPU process present into another process's child window, which is how Chromium/CEF draws ([DXMT #141](https://github.com/3Shain/dxmt/issues/141)). |

Both are rebased from [codeWonderland/neutron](https://github.com/codeWonderland/neutron) (MIT, AI-assisted per its author); each file's header has the details.

DXMT itself still refuses cross-process swapchains, so [`nix/dxmt-cross-process.py`](nix/dxmt-cross-process.py) NOPs that one branch in the prebuilt v0.80 `d3d11.dll` (64- and 32-bit). It checks file hashes and surrounding bytes, so **bumping DXMT fails the build until the offsets are redone** for the new release.

The `crossover` fork (Wine 11.0) doesn't have the remote-layer code these patches build on, so CEF stays black there.

## Conformance tests

The daily [WineTest](https://gitlab.winehq.org/wine/wine/-/wikis/Conformance-Tests) executables are pinned with nvfetcher (`nix build .#winetest` holds both). Arguments go straight to `winetest.exe` / `winetest64.exe`:

```bash
just winetest64 -h                                  # list WineTest's options
just winetest64 -c -o out64.report d3d11 dxgi       # selected tests, report kept locally
just winetest32 -c -m you@example.com -o out32.report   # full run needs -m
```

Use `-o` to keep the report local: DXMT replaces Wine's d3d DLLs, so this counts as a patched build and test.winehq.org won't accept its results. Set `WINEPREFIX` to keep test runs out of your normal prefix.

## Debugging

> [!TIP]
> Run compile commands by hand only inside `just shell`. Outside it, the host's `clang`/`ld` gets picked up and the errors mislead you (e.g. `no such file or directory: 'libgcc.a'` or `invalid linker name '-fuse-ld=lld'`).

<details>
<summary><b><code>winegcc</code> / <code>winebuild</code> errors</b></summary>

```bash
nix develop --command just shell

# Inside the build shell:
tools/winegcc/winegcc -o dlls/atmlib/x86_64-windows/atmlib.dll \
  --wine-objdir . -b x86_64-w64-mingw32 -Wl,--wine-builtin -shared \
  dlls/atmlib/atmlib.spec -Wb,--prefer-native dlls/atmlib/x86_64-windows/main.o \
  dlls/winecrt0/x86_64-windows/libwinecrt0.a dlls/ucrtbase/x86_64-windows/libucrtbase.a \
  dlls/kernel32/x86_64-windows/libkernel32.a dlls/ntdll/x86_64-windows/libntdll.a
```

</details>

<details>
<summary><b>Validating msync</b></summary>

```bash
WINEMSYNC=1 WINEDEBUG="+msync" ./output/wow64/usr/local/bin/wine winecfg
# Expected output: msync: up and running.
```

</details>

<details>
<summary><b><code>kernel32.dll</code> panic (GStreamer deadlock)</b></summary>

```text
wine: could not load kernel32.dll, status c0000135
```

To check whether GStreamer is breaking prefix initialisation:

```bash
rm -rf ~/.wine                                                       # remove the broken prefix
WINEDLLOVERRIDES="winegstreamer=d" ./output/wow64/usr/local/bin/wine clock   # boot without GStreamer
```

If clock opens now, GStreamer's dependencies are either missing from the bundle or deadlocking against host libraries. See [WineHQ #51086](https://bugs.winehq.org/show_bug.cgi?id=51086).

</details>

<details>
<summary><b>GnuTLS</b></summary>

Find where `dlopen` fails:

```bash
WINEDEBUG=+module ./output/wow64/usr/local/bin/wine iexplore.exe "https://google.com" 2>&1 | grep -E "(dlopen|gnutls)"
```

The failure looks like this:

```text
0244:err:winediag:process_attach Failed to load libgnutls, secure connections will not be available.
```

Check the libraries are in the bundle and linked to Wine's crypto modules:

```bash
cd ./output/wow64/usr/local/lib/wine/x86_64-unix
ls -l | grep gnutls                 # what's actually bundled
otool -L bcrypt.so | grep gnutls    # crypto modules (not avicap32.so, that's webcams)
otool -L crypt32.so | grep gnutls
otool -L avicap32.so                # general dylib linkage and loader paths
```

To test whether `dyld` can load the library without Wine, use an x86_64 Python from the `nixpkgs-26.05-darwin` channel. The host's `arch -x86_64 python3` is not equivalent.

```bash
nix shell --system x86_64-darwin nixpkgs/nixpkgs-26.05-darwin#python3
python3 -c "import ctypes; ctypes.cdll.LoadLibrary('./libgnutls.30.dylib')" || \
python3 -c "import ctypes; ctypes.cdll.LoadLibrary('./libgnutls.dylib')"
```

</details>

<details>
<summary><b>Validating MoltenVK</b></summary>

```bash
env MVK_CONFIG_LOG_LEVEL=3 /path_to/wine /path_to/dxcapsviewer.exe
```

Expected output:

```text
[mvk-info] MoltenVK version 1.4.1, supporting Vulkan version 1.4.341.
  The following 153 Vulkan extensions are supported:
  VK_KHR_16bit_storage v1
  ...
[mvk-info] GPU device:
  model: Apple M2
  type: Integrated
  vendorID: 0x106b
  ...
```

</details>

<details>
<summary><b>Running with DXMT</b></summary>

Grab the [DxCapsViewer](https://github.com/microsoft/DxCapsViewer) release `.exe` for testing.

```bash
WINE="./output/wow64/usr/local/bin/wine"
export WINEPREFIX="$HOME/.wine"
export WINEDLLOVERRIDES="dxgi,d3d11,d3d10core=b"
export WINEDEBUG=+loaddll
export MTL_HUD_ENABLED=1

"$WINE" /path/to/dxcapsviewer.exe 2>&1 | grep -iE "err:module|winemetal|d3d11|dxgi"
```

DXMT 0.8 only supports DirectX 10/11. D3D9 falls back to `wined3d` and logs `wined3d_guess_card_vendor "Apple"`; that's expected.

</details>

<details>
<summary><b>Testing Chromium / CEF (Steam's UI)</b></summary>

Download the **Sample Application** (`cefclient`) for Windows 64-bit from the [CEF builds](https://cef-builds.spotifycdn.com/index.html) page and unpack it to `/tmp/cef`. It draws through the same out-of-process GPU path as Steam's UI, without an account or launcher in the way.

Run it in a throwaway prefix:

```bash
WINEPREFIX=/tmp/cefprefix ./output/wow64/usr/local/bin/wine \
  /tmp/cef/Release/cefclient.exe --url=chrome://gpu --enable-logging=stderr 2> /tmp/cef.log
```

Working: the page draws, `chrome://gpu` lists hardware acceleration, and resizing doesn't leave the window black. If it stays black, find where it stops:

```bash
grep -c 'cross-process swapchain not supported' /tmp/cef.log   # DXMT refusal: the dxmt-cross-process.py patch is missing
grep -ci 'gpu process.*exit\|crash' /tmp/cef.log                # GPU process crash-looping
WINEDEBUG=+macdrv ./output/wow64/usr/local/bin/wine /tmp/cef/Release/cefclient.exe 2>&1 | grep 'Cross-process child'
                                                                # winemac 0002 patch missing (stock winemac)
```

If pages draw but show no text, check for `cannot find the FreeType font library` in the log. Without FreeType Wine still reports fonts but can't rasterise them, which looks like a compositing bug in a screenshot.

`--disable-gpu` (Steam: `-cef-disable-gpu`) falls back to software rendering, which is useful as a control.

</details>

## License

[GPL-3.0-only](LICENSES/GPL-3.0-only.txt). Generated and config files are [CC0-1.0](LICENSES/CC0-1.0.txt) (see [`REUSE.toml`](REUSE.toml)).

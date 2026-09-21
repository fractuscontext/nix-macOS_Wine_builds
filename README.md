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
- [ ] OpenGL 4.2+ (Not Working)

Two source forks are available:

| Fork | Source | Release tag looks like |
| --- | --- | --- |
| `latest` | The upstream WineHQ release tag, checked daily | `wine-11.20-…` |
| `crossover` | CodeWeavers' CrossOver sources | `cx-26.3.0-…` |

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
just bundle         # Copies dylib to make `output/wow64` is self-contained and portable.
```

```bash
./output/wow64/usr/local/bin/wine winecfg
```

Other commands:

| Command | What it does |
| --- | --- |
| `just shell` | Opens a shell with the exact build environment, for running compile commands by hand |
| `just update-source [pin]` | Updates the source pins with nvfetcher, either all of them or one (`wine-latest`, `crossover-source`) |
| `just src=../wine out=/tmp/wow64 all` | Builds a different source tree into a different output directory |

Releases are tagged `<flavour>-<yy>.<mmdd>.<hhmm>` in UTC, e.g. `wine-11.20-26.1026.1932` or `cx-26.3.0-26.1026.1932`.

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

## License

[GPL-3.0-only](LICENSES/GPL-3.0-only.txt). Generated and config files are [CC0-1.0](LICENSES/CC0-1.0.txt) (see [`REUSE.toml`](REUSE.toml)).

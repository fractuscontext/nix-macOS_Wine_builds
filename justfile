# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

# Run inside `nix develop`: store paths arrive as WINE_* env vars (see nix/toolchain.nix).

set shell := ["bash", "-euo", "pipefail", "-c"]

# Override on the command line, e.g. `just src=../my-wine out=/tmp/wow64 all`
src := "sources/wine"
out := "output/wow64"

src_dir := absolute_path(src)
install_dir := absolute_path(out)
wine_lib := install_dir / "usr/local/lib/wine"
ccache_dir := justfile_directory() / ".ccache"

# GitHub Actions sets CI=true; keep compiler output quiet there
verbose := if env("CI", "") == "true" { "" } else { "-v" }

macos_min := "15.0"
sdkroot := env("WINE_SDKROOT", "")
toolchain := env("WINE_TOOLCHAIN_PATH", "")
target_flags := "-target x86_64-apple-darwin -mmacosx-version-min=" + macos_min + " -isysroot " + sdkroot

export PATH := if toolchain == "" { env("PATH") } else { ccache_dir / "bin" + ":" + toolchain + ":" + env("PATH") }
export MACOSX_DEPLOYMENT_TARGET := macos_min
export SDKROOT := sdkroot
export PKG_CONFIG_PATH := env("WINE_PKG_CONFIG_PATH", "") + ":" + env("PKG_CONFIG_PATH", "")
export CFLAGS := target_flags
export LDFLAGS := target_flags + " " + env("WINE_LIB_DIRS", "") + " -F" + sdkroot + "/System/Library/Frameworks"

export CCACHE_DIR := ccache_dir
# Base directory for relative path rewrites to maximize cache hits
export CCACHE_BASEDIR := src_dir
export CCACHE_NOHASHDIR := "1"
export CCACHE_COMPILERCHECK := "string:" + env("WINE_CLANG", "")
export CCACHE_MAXSIZE := "5G"

export xcrun_log := "1"
export xcrun_verbose := "1"
export xcrun_nocache := "1"

[private]
default:
    @just --list

# Bump the pins in _sources/ with nvfetcher, or only one, e.g. `just update-source wine-latest`
[group('source')]
update-source pin="":
    nvfetcher -c nvfetcher.toml {{ if pin == "" { "" } else { "--filter '^" + pin + "$'" } }}

# Copy a pinned source tree (nix/sources.nix) out of the Nix store: crossover | latest
[group('source')]
clone flavour="latest" target=src:
    #!/usr/bin/env bash
    set -euo pipefail
    tree=$(nix build --no-link --print-out-paths ".#source-{{ flavour }}")
    rm -rf "{{ target }}"
    mkdir -p "$(dirname "{{ target }}")"
    cp -R "$tree" "{{ target }}"
    chmod -R u+w "{{ target }}"
    echo "[*] Cloned $(cat "{{ target }}/WINE_FLAVOUR") to {{ target }}"

# Decide what CI does for this commit and do it (see scripts/ci.py)
[group('ci')]
ci:
    python3 scripts/ci.py

# Open a shell with the build environment, for running compile commands by hand
[group('build')]
shell: ccache-wrappers
    exec bash -i

# configure → make → install → install-dxmt → bundle
[group('build')]
all: configure make install install-dxmt bundle
    @echo "Build pipeline completed successfully."

# Configure Wine for WoW64 (x86_64 + i386)
[group('build')]
configure: ccache-wrappers
    #!/usr/bin/env bash
    set -euo pipefail
    ulimit -n 4096 || true; ulimit -u 2048 || true

    cd "{{ src_dir }}"
    if [ -f Makefile ]; then make clean; fi

    CC=clang CXX=clang++ \
    i386_CC=clang i386_CXX=clang++ \
    i386_CFLAGS="-march=nehalem -O2" \
    i386_CXXFLAGS="-march=nehalem -O2" \
    x86_64_CC=clang x86_64_CXX=clang++ \
    x86_64_CFLAGS="-march=x86-64-v2 -mtune=nehalem -O2" \
    x86_64_CXXFLAGS="-march=x86-64-v2 -mtune=nehalem -O2" \
    ./configure \
      --build=x86_64-apple-darwin \
      --enable-archs=x86_64,i386 \
      --disable-winedbg \
      --disable-tests \
      --disable-winebth_sys \
      --disable-win16 \
      --with-coreaudio \
      --with-ffmpeg \
      --with-freetype \
      --with-gettext \
      --with-gnutls \
      --with-gstreamer \
      --with-pthread \
      --with-sdl \
      --with-vulkan \
      --without-alsa \
      --without-capi \
      --without-cups \
      --without-dbus \
      --without-fontconfig \
      --without-gettextpo \
      --without-gphoto \
      --without-gssapi \
      --without-hwloc \
      --without-inotify \
      --without-krb5 \
      --without-netapi \
      --without-opencl \
      --without-oss \
      --without-pcap \
      --without-pcsclite \
      --without-pulse \
      --without-sane \
      --without-udev \
      --without-unwind \
      --without-usb \
      --without-v4l2 \
      --without-wayland \
      --without-x

# Compile Wine
[group('build')]
make: ccache-wrappers
    #!/usr/bin/env bash
    set -euo pipefail
    ulimit -n 4096 || true; ulimit -u 2048 || true
    make -C "{{ src_dir }}" -j"$(nproc)"

# Install the runtime into the output prefix (no headers, winegcc/winebuild or import libs)
[group('build')]
install: ccache-wrappers
    mkdir -p "{{ install_dir }}"
    make -C "{{ src_dir }}" install-lib DESTDIR="{{ install_dir }}"

# Overwrite Wine's builtin d3d DLLs and Unix SO with DXMT's
[group('build')]
install-dxmt: require-install
    cp -f "$WINE_DXMT/x86_64-unix/winemetal.so" "{{ wine_lib }}/x86_64-unix/"
    cp -f "$WINE_DXMT/x86_64-windows/"*.dll "{{ wine_lib }}/x86_64-windows/"
    cp -f "$WINE_DXMT/i386-windows/"*.dll "{{ wine_lib }}/i386-windows/"

# Bundle external runtime dylibs next to Wine's Unix libraries
[group('build')]
bundle: require-install
    wine-bundle-libs "{{ wine_lib }}/x86_64-unix"

[private]
require-install:
    @[ -d "{{ wine_lib }}/x86_64-unix" ] || { echo "Error: {{ wine_lib }} not found. Run 'just install' first."; exit 1; }

[private]
ccache-wrappers:
    #!/usr/bin/env bash
    set -euo pipefail
    : "${WINE_CLANG:?Run this inside nix develop on macOS}"

    rm -rf "{{ ccache_dir }}/bin"
    mkdir -p "{{ ccache_dir }}/bin"
    ccache=$(command -v ccache)

    for pair in clang:clang clang++:clang++ cc:clang c++:clang++; do
      wrapper="{{ ccache_dir }}/bin/${pair%%:*}"
      printf '#!/usr/bin/env bash\nexec %q %q {{ verbose }} "$@"\n' \
        "$ccache" "$WINE_CLANG/bin/${pair#*:}" > "$wrapper"
      chmod +x "$wrapper"
    done

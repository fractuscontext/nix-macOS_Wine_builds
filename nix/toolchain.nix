# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

# Everything the justfile needs from the Nix store, exposed as WINE_* env vars.
# Recipes compose the actual CFLAGS/LDFLAGS/PATH so the interactive shell stays clean.
{
  buildPkgs,
  targetPkgs,
  mac-bundler,
}:

let
  inherit (buildPkgs) lib;

  dxmtPrecompiled = buildPkgs.fetchzip {
    url = "https://github.com/3Shain/dxmt/releases/download/v0.80/dxmt-v0.80-builtin.tar.gz";
    sha256 = "sha256-Ckrb/7B3+COEfu3iaqsXK1q0L9KbwIsUmEegSNbiznQ=";
  };

  baseDeps = with targetPkgs; [
    freetype
    libpng
    zlib
    gnutls
    lcms2
    SDL2
    ffmpeg
    gst_all_1.gstreamer
    gst_all_1.gst-plugins-base
    glib
    pcre2
    libffi
    vulkan-loader
    moltenvk
  ];

  pluginDeps = with targetPkgs; [
    gst_all_1.gst-plugins-good
    gst_all_1.gst-plugins-bad
    gst_all_1.gst-plugins-ugly
    gst_all_1.gst-libav
    gmp
    p11-kit
    bzip2
    libidn2
    libtasn1
  ];

  # bundleLibs generates shell code at eval time, so it has to stay a Nix-built script
  wine-bundle-libs = buildPkgs.writeShellScriptBin "wine-bundle-libs" ''
    set -euo pipefail

    if [ $# -ne 1 ] || [ ! -d "$1" ]; then
      echo "Usage: wine-bundle-libs <wine x86_64-unix dir>"
      exit 1
    fi

    # Bundler and otool run natively on the build host; only the bundled libs are x86_64
    ${mac-bundler.lib.bundleLibs {
      pkgs = buildPkgs;
      targetPackages = baseDeps ++ pluginDeps;
      outPath = "$1";
      quiet = false;
    }}

    ln -sfn libMoltenVK.dylib "$1/libvulkan.1.dylib"
  '';
in
{
  packages = [
    buildPkgs.ccache
    wine-bundle-libs
  ];

  env = {
    WINE_TOOLCHAIN_PATH = lib.makeBinPath (
      with buildPkgs;
      [
        coreutils
        stdenv.cc
        flex
        # flex shells out to m4; without it macOS's /usr/bin/m4 xcrun stub is hit
        m4
        bison
        pkg-config
        gnumake
        gettext
        darwin.cctools
        gawk
        llvmPackages.bintools-unwrapped
        llvmPackages.lld
      ]
    );
    WINE_CLANG = "${buildPkgs.llvmPackages.clang-unwrapped}";
    WINE_SDKROOT = "${targetPkgs.apple-sdk_15}/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk";
    WINE_PKG_CONFIG_PATH = lib.makeSearchPathOutput "dev" "lib/pkgconfig" baseDeps;
    WINE_LIB_DIRS = "-L${targetPkgs.moltenvk}/lib -L${targetPkgs.vulkan-loader}/lib";
    WINE_DXMT = "${dxmtPrecompiled}";
  };
}

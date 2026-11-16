# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

# Pinned Wine source trees, normalised to a plain directory with a WINE_FLAVOUR
# file (used as the release tag). `just clone <flavour>` copies one out.
{ pkgs }:

let
  pins = pkgs.callPackage ../_sources/generated.nix { };

  mkSourceTree =
    tag: script:
    pkgs.runCommand "wine-source-${tag}" { nativeBuildInputs = [ pkgs.pigz ]; } ''
      ${script}
      echo "${tag}" > $out/WINE_FLAVOUR
    '';

  mkMono =
    pin:
    pkgs.runCommand "wine-mono-${pin.version}" { } ''
      mkdir -p $out
      cp -R ${pin.src} $out/wine-mono-${pin.version}
    '';
in
{
  # CrossOver ships one big tarball with Wine under sources/wine
  source-crossover =
    with pins.crossover-source;
    mkSourceTree "cx-${version}" ''
      mkdir -p $out
      tar -I pigz -xf ${src} -C $out --strip-components=2 sources/wine
    '';

  # Wine Mono per source fork, laid out as Wine's share/wine/mono expects:
  # $out/wine-mono-<version>/. `just install-mono` copies the matching one.
  mono-latest = mkMono pins.wine-mono-latest;
  mono-crossover = mkMono pins.wine-mono-crossover;

  # Newest WineHQ release tag, already unpacked by fetchTarball
  # Patches in patches/wine-latest/ are applied in name order (see each file's header)
  source-latest =
    with pins.wine-latest;
    mkSourceTree "wine-${version}" ''
      cp -R ${src} $out
      chmod -R u+w $out
      for p in ${../patches/wine-latest}/*.patch; do
        echo "applying $(basename "$p")"
        ${pkgs.patch}/bin/patch -d $out -p1 --forward --batch < "$p"
      done
    '';
}

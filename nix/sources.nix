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
in
{
  # CrossOver ships one big tarball with Wine under sources/wine
  source-crossover =
    with pins.crossover-source;
    mkSourceTree "cx-${version}" ''
      mkdir -p $out
      tar -I pigz -xf ${src} -C $out --strip-components=2 sources/wine
    '';

  # Newest WineHQ release tag, already unpacked by fetchTarball
  source-latest =
    with pins.wine-latest;
    mkSourceTree "wine-${version}" ''
      cp -R ${src} $out
      chmod u+w $out
    '';
}

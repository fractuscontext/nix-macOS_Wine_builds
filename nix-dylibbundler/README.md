<!--
SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>

SPDX-License-Identifier: GPL-3.0-only
-->

# nix-dylibbundler

For those who build software with Nix, but distribute the traditional way.

If your CI/CD builds with Nix but you need to ship a standalone, relocatable
version of your software, you hit the same wall every time: the binaries link
against libraries in `/nix/store`, which won't exist on the target machine.

`nix-dylibbundler` recursively discovers, copies, and relinks those dynamic
libraries into a single flat directory so the result is self-contained and
relocatable.

## Usage

### Via CLI

Run this to copy the dependencies and rewrite their install names so they
resolve relative to the bundle:

```bash
nix run ".#" -- \
  -p $(nix build nixpkgs#gnutls^lib --no-link --print-out-paths) \
  -p $(nix build nixpkgs#curl^lib --no-link --print-out-paths) \
  -o /tmp/the_deps_i_need
```

This fetches the dynamic libraries for GnuTLS and cURL, along with their
recursive dependencies (like `libnettle`), and copies them into your target
directory.

Here's what the command does:

- relinks the Mach-O files already in the outpath (e.g. Wine's `*.so`), so
  their `/nix/store` references point into the bundle
- follows absolute, `@rpath/` and `@loader_path/` references, plus libraries
  that are only reachable through an `LC_RPATH` directory (things loaded with
  `dlopen()`, such as SDL3 behind sdl2-compat)
- copies the libraries into the outpath as `{nix-hash}-{basename}.dylib|.so`
- rewrites each reference to `@loader_path/{hashed-name}` and drops
  `/nix/store` rpaths
- adds a plain `{basename}` symlink for every name a library was reached
  through, so `dlopen("libfoo.1.dylib")` lookups keep working
- re-reads every Mach-O file in the outpath from disk and fails if any
  reference would not resolve on a machine without `/nix/store`

#### Options

| Flag | Description |
| --- | --- |
| `-o`, `--outpath` | Target directory for bundled libraries. Must already exist. |
| `-p`, `--pkgs` | Package root to traverse. Repeatable. Must be an existing path. |
| `--seed-glob` | Glob (relative to `--outpath`) selecting the Mach-O files already in the outpath to relink. Defaults to `*`; pass `''` to leave them alone (verification still checks them). |
| `--scan-all` | Scan the whole package root instead of just `<pkg>/lib`. |
| `-q`, `--quiet` | Suppress verbose logging output. |

By default only `<pkg>/lib` is walked, so `bin/`, `libexec/` and `share/` stay
out of the bundle. Use `--scan-all` if a package ships libraries elsewhere.

### As a Nix library function

Functionally the same as CLI utility. Call it as a Nix library function
if the CLI approach is too repetitive:

```nix
inputs = {
  nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  mac-bundler.url = "github:NixOS/fractuscontext/nix-dylibbundler";
  mac-bundler.inputs.nixpkgs.follows = "nixpkgs";
};

outputs = { self, nixpkgs, mac-bundler }: {
  # Inside your derivation or build step:
  buildPhase = ''
    ${mac-bundler.lib.bundleLibs {
      inherit pkgs;                  # should follow nixpkgs
      targetPackages = with pkgs; [  # packages whose libraries to bundle
        curl
        gnutls
      ];
      outPath = "abs_path_to_folder";  # target directory (must already exist)
      quiet = false;                   # verbose logging
      scanAll = true;                  # scan the whole package root, not just <pkg>/lib
      seedGlob = "*.so";               # outPath files to relink (default "*")
    }}
  '';
};

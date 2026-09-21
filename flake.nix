# SPDX-FileCopyrightText: 2026 Claire Tam <claire2026t@posteo.net>
#
# SPDX-License-Identifier: GPL-3.0-only

{
  inputs = {
    nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?shallow=1&ref=nixos-unstable";
    nixpkgs-darwin.url = "git+https://github.com/NixOS/nixpkgs?shallow=1&ref=nixpkgs-26.05-darwin";

    mac-bundler = {
      url = "github:fractuscontext/nix-dylibbundler";
      inputs.nixpkgs.follows = "nixpkgs-darwin";
    };

    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      nixpkgs-darwin,
      mac-bundler,
      git-hooks,
      self,
    }:
    let
      # We added linux architectures so GitHub's ubuntu-latest runners
      # can evaluate the flake and run the 'just update-source' cronjob.
      # Builds run on aarch64-darwin and target x86_64-darwin (targetPkgs),
      # so x86_64-darwin is deliberately not a host system here.
      systems = [
        "aarch64-darwin"
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      # Expose the hooks as flake checks so `nix flake check` works naturally
      checks = forAllSystems (
        system:
        let
          buildPkgs = import nixpkgs { inherit system; };
        in
        {
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              nixfmt.enable = true;

              actionlint.enable = true;

              rumdl = {
                enable = true;
                name = "Markdown Lint Check (rumdl)";
                entry = "${buildPkgs.rumdl}/bin/rumdl check";
                types = [ "markdown" ];
                pass_filenames = false;
              };

              reuse = {
                enable = true;
                name = "SPDX License Check";
                entry = "${buildPkgs.reuse}/bin/reuse lint";
                pass_filenames = false;
              };
            };
          };
        }
      );

      # Pinned source trees: `nix build .#source-crossover` / `.#source-latest`
      packages = forAllSystems (
        system: import ./nix/sources.nix { pkgs = import nixpkgs { inherit system; }; }
      );

      devShells = forAllSystems (
        system:
        let
          buildPkgs = import nixpkgs { inherit system; };
          targetPkgs = import nixpkgs-darwin { system = "x86_64-darwin"; };
          inherit (buildPkgs) lib;
          isDarwin = buildPkgs.stdenv.isDarwin;

          # Only forced on macOS, avoiding cross-OS evaluation errors
          toolchain = import ./nix/toolchain.nix { inherit buildPkgs targetPkgs mac-bundler; };
        in
        {
          default = buildPkgs.mkShell {
            name = "wow64-dev-env";

            packages =
              with buildPkgs;
              [
                bashInteractive
                just
                nvfetcher
                git
                python3
              ]
              ++ lib.optionals isDarwin toolchain.packages;

            env = lib.optionalAttrs isDarwin toolchain.env;

            shellHook = ''
              ${self.checks.${system}.pre-commit-check.shellHook}
              echo "🍷 Loaded Wow64 build environment"
              if [ "${lib.boolToString isDarwin}" = "false" ]; then
                echo "Notice: You are on Linux. Only 'just update-source' and 'just clone' are available here."
              else
                echo "Standard Usage:"
                echo "  just update-source"
                echo "  just clone crossover"
                echo "  just all"
              fi
            '';
          };
        }
      );
    };
}

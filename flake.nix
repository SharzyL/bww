{
  description = "bww - BubbleWrap Wrapper";

  inputs = {
    nixpkgs.url = "nixpkgs";
    flake-parts.url = "flake-parts";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { flake-parts, ... }@inputs:
    let
      name = "bww";
      makePkg = { lib, buildPythonPackage, uv-build, kdl-py, pytest }:
        buildPythonPackage {
          pname = name;
          version = "0.1.0";
          pyproject = true;
          nativeBuildInputs = [ uv-build ];

          propagatedBuildInputs = [
            kdl-py
          ];

          checkInputs = [
            pytest
          ];

          src = with lib.fileset; toSource {
            root = ./.;
            fileset = fileFilter
              (file: ! (lib.elem file.name [ "flake.nix" "flake.lock" ]))
              ./.;
          };
          meta.mainProgram = "bww";
        };

      shellOverride = pkgs: oldAttrs: {
        name = "${name}-dev-shell";
        version = null;
        src = null;
        nativeBuildInputs = (oldAttrs.nativeBuildInputs or [ ]) ++ (with pkgs; [
          uv
          ty
          ruff

          tun2socks
          bubblewrap
          passt
        ]);
        # to allow lsp to find python
        shellHook = ''
          export PATH="$PWD/.venv/bin:$PATH"
        '';
      };
      overlay = final: _: {
        ${name} = final.python3Packages.callPackage makePkg { };
      };

    in
    # flake-parts boilerplate
    flake-parts.lib.mkFlake { inherit inputs; } {
      imports = [
        inputs.treefmt-nix.flakeModule
      ];

      flake.overlays.default = overlay;

      systems = inputs.nixpkgs.lib.systems.flakeExposed;

      perSystem = { system, config, pkgs, ... }: {
        packages.default = config.legacyPackages.${name};
        packages.${name} = config.packages.default;
        devShells.default = config.packages.default.overrideAttrs (shellOverride pkgs);
        legacyPackages = pkgs;

        _module.args.pkgs = import inputs.nixpkgs {
          inherit system;
          overlays = [ overlay ];
        };

        treefmt = {
          programs.ruff-format.enable = true;
          programs.mypy = {
            enable = true;
            directories.".".extraPythonPackages = config.packages.default.propagatedBuildInputs;
          };
          programs.nixpkgs-fmt.enable = true;
        };
      };
    };
}

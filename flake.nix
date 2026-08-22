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
      makePkg = { lib, buildPythonPackage, uv-build, kdl-py, pytest, loguru }:
        buildPythonPackage {
          pname = name;
          version = "0.1.0";
          pyproject = true;
          nativeBuildInputs = [ uv-build ];

          propagatedBuildInputs = [
            kdl-py
            loguru
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
        # Keep the inherited `version` (a string): newer nixpkgs
        # mk-python-derivation evaluates `lib.hasInfix "unstable-"
        # version` for pyproject packages, which chokes on `null`.
        src = null;
        nativeBuildInputs = (oldAttrs.nativeBuildInputs or [ ]) ++ (with pkgs; [
          uv
          ty
          ruff

          tun2socks
          bubblewrap
          passt
        ]);
        # `uv` keeps the venv in `.venv`; everything we need (including
        # the git-pinned kdl-py v2) lives there. Nix's Python dev-shell
        # also injects propagated deps via PYTHONPATH — including
        # `kdl-py 1.2.0` from nixpkgs, which would shadow the venv's v2
        # build and break parsing (`Node.entries` is v2-only). Unset
        # PYTHONPATH so the venv's site-packages wins.
        shellHook = ''
          export PATH="$PWD/.venv/bin:$PATH"
          unset PYTHONPATH
        '';
      };
      # Override the nixpkgs-packaged kdl-py to point at tabatkins/kdlpy
      # main, which supports KDL v2 (triple-quoted strings etc.). Keep
      # this in sync with the git revision pinned in pyproject.toml /
      # uv.lock — otherwise the dev shell's Nix-injected kdl-py 1.2.0
      # shadows the venv's git build and you'll see v1 parse errors.
      kdlpyRev = "d9a220762fb9f55e4f59296256221084c26f54da";
      pythonOverrides = pyFinal: pyPrev: {
        kdl-py = pyPrev.kdl-py.overrideAttrs (old: {
          # A git snapshot: base 1.2.0 (what upstream's METADATA still
          # declares) plus the rev as a PEP 440 local-version segment.
          version = "1.2.0+unstable.${builtins.substring 0 7 kdlpyRev}";
          # ...which no longer equals the METADATA's bare '1.2.0', so the
          # metadata-version check would fail. Skip it — the rev in the
          # store path is the useful signal here, not METADATA parity.
          dontCheckPythonMetadata = true;
          src = pyPrev.pkgs.fetchFromGitHub {
            owner = "tabatkins";
            repo = "kdlpy";
            rev = kdlpyRev;
            # Run `nix build` once with `hash = lib.fakeHash` to get the
            # real value, then paste it here.
            hash = "sha256-UttkZOkimu58ctDaH2o1Vv+oYLFute8Dwc1eaToeArE=";
          };
          # nixpkgs' kdl-py 1.2.0 derivation runs `python tests/run.py`
          # as installCheck; tabatkins/kdlpy main reorganised its tests
          # so that file no longer exists. Skip the check rather than
          # adapt — we only need the package importable.
          doCheck = false;
          doInstallCheck = false;
        });
      };
      overlay = final: prev: {
        python3 = prev.python3.override (old: {
          packageOverrides = prev.lib.composeExtensions (old.packageOverrides or (_: _: { })) pythonOverrides;
        });
        python3Packages = final.python3.pkgs;
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

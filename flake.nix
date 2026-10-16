{
  description = "BA development shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      # OpenCodeReview (ocr) CLI, pinned to a release; bump version + hashes together.
      ocrVersion = "1.13.0";
      ocrAssets = {
        x86_64-linux = "linux-amd64";
        aarch64-linux = "linux-arm64";
      };
      ocrHashes = {
        x86_64-linux = "sha256-h51u2ifRNkc/evaA7VAJUWuvYjbYoOX18U8+bezs3R8=";
        aarch64-linux = "sha256-LWbHHu6ghzDhOUzDmIS5ZE0wEohEPWRKAsKt67BCW5M=";
      };
      mkOcr = pkgs:
        let
          system = pkgs.stdenv.hostPlatform.system;
        in
        pkgs.stdenvNoCC.mkDerivation {
          pname = "open-code-review";
          version = ocrVersion;
          src = pkgs.fetchurl {
            url = "https://github.com/alibaba/open-code-review/releases/download/v${ocrVersion}/opencodereview-${ocrAssets.${system}}";
            hash = ocrHashes.${system};
          };
          dontUnpack = true;
          installPhase = "install -Dm755 $src $out/bin/ocr";
          meta = {
            description = "AI-powered code review CLI (OpenCodeReview)";
            homepage = "https://github.com/alibaba/open-code-review";
            license = pkgs.lib.licenses.asl20;
            platforms = [ "x86_64-linux" "aarch64-linux" ];
            mainProgram = "ocr";
          };
        };
    in {
      packages = forAllSystems (pkgs: {
        ocr = mkOcr pkgs;
      });
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [
            (pkgs.python3.withPackages (ps: [ ps.prompt-toolkit ps.pygments ]))
            pkgs.ruff
            (mkOcr pkgs)
          ];
        };
      });
    };
}

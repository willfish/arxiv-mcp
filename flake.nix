{
  description = "Native arXiv search CLI and small stdio MCP adapter";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  outputs = { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forEach = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs { inherit system; }));
    in {
      packages = forEach (pkgs: {
        maintenance = pkgs.callPackage ./packaging/python-tools.nix { };
        default = pkgs.stdenv.mkDerivation {
          pname = "arxiv-mcp";
          version = "0.1.0";
          src = ./.;
          nativeBuildInputs = [ pkgs.pkg-config pkgs.makeWrapper ];
          buildInputs = [ pkgs.cjson pkgs.sqlite pkgs.zlib ];
          doCheck = true;
          checkPhase = "make test";
          installPhase = ''
            make install PREFIX="$out"
            wrapProgram "$out/bin/arxiv-mcp" --set-default ARXIV_BINARY "$out/bin/arxiv"
          '';
        };
      });
      devShells = forEach (pkgs: {
        default = pkgs.mkShell {
          nativeBuildInputs = [ pkgs.pkg-config ];
          buildInputs = [ pkgs.cjson pkgs.sqlite pkgs.zlib ];
        };
      });
    };
}

{
  description = "GameFrame PS2 integer-function verification on NixOS";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/f4b794aaffc0e3d45da81088d492b118224bb476";
  outputs = { self, nixpkgs }: let
    pkgs = import nixpkgs { system = "x86_64-linux"; };
  in {
    devShells.x86_64-linux.default = pkgs.mkShell {
      packages = [ (pkgs.python3.withPackages (p: [ p.unicorn p.capstone p.pefile ]))
                   pkgs.gcc pkgs.zig pkgs.openjdk21 pkgs.ghidra pkgs.zip pkgs.unzip ];
      # A fixed modern PE link date for upstream's build-identity fixture.
      SOURCE_DATE_EPOCH = "1767225600";
      PYTHON = "${pkgs.python3.withPackages (p: [ p.unicorn p.capstone p.pefile ])}/bin/python";
      JAVA_HOME = "${pkgs.openjdk21}";
      GHIDRA_INSTALL_DIR = "${pkgs.ghidra}/lib/ghidra";
    };
  };
}

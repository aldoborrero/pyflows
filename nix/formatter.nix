{
  inputs,
  pkgs,
  ...
}:
inputs.treefmt-nix.lib.mkWrapper pkgs {
  projectRootFile = "flake.nix";
  programs = {
    nixfmt.enable = true;
    ruff-check.enable = true;
    ruff-format.enable = true;
  };
}

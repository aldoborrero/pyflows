{
  pkgs,
  perSystem,
  ...
}:
pkgs.mkShellNoCC {
  name = "pyflows";
  packages = with pkgs; [
    python313
    ruff
    mypy
    ffmpeg-full
  ];

  shellHook = ''
    export PRJ_ROOT="$PWD"
  '';
}

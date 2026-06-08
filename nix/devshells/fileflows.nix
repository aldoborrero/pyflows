{
  pkgs,
  ...
}:
pkgs.mkShellNoCC {
  name = "fileflows-analysis";

  packages = with pkgs; [
    dotnet-sdk_8
    ilspycmd
    unzip
    file
    jq
    tree
    binutils
    ripgrep
    sqlite
  ];

  shellHook = ''
    export PRJ_ROOT="$PWD"
  '';
}

{
  pkgs,
  ...
}:
with pkgs;

python313.pkgs.buildPythonApplication {
  pname = "pyflows";
  version = "0.1.0";
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../pyflows
      ../tests
    ];
  };

  build-system = with python313.pkgs; [
    setuptools
  ];

  dependencies = with python313.pkgs; [
    click
    pydantic
    pyyaml
    huey
    watchdog
    rich
    prometheus-client
  ];

  nativeBuildInputs = [ makeWrapper ];

  postFixup = ''
    wrapProgram $out/bin/pyflows \
      --prefix PATH : ${lib.makeBinPath [ ffmpeg-full ]}
  '';

  nativeCheckInputs = with python313.pkgs; [
    pytestCheckHook
  ] ++ [ ffmpeg-full ];

  disabledTests = [];

  meta = with lib; {
    description = "Media library transcoder with VAAPI hardware encoding";
    license = licenses.mit;
    sourceProvenance = with sourceTypes; [ fromSource ];
    mainProgram = "pyflows";
    platforms = platforms.linux;
  };
}

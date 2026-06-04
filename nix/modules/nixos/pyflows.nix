{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.pyflows;
  settingsFormat = pkgs.formats.yaml { };
  configFile = settingsFormat.generate "pyflows.yaml" cfg.settings;
in
{
  options.services.pyflows = {
    enable = lib.mkEnableOption "pyflows media transcoder";

    package = lib.mkPackageOption pkgs "pyflows" { };

    user = lib.mkOption {
      type = lib.types.str;
      default = "pyflows";
      description = "User account under which pyflows runs.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "pyflows";
      description = "Group under which pyflows runs.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/pyflows";
      description = "Directory for pyflows state (database, logs).";
    };

    settings = lib.mkOption {
      type = settingsFormat.type;
      default = { };
      description = ''
        pyflows configuration. See config.example.yaml for available options.
        This will be rendered as YAML and passed to pyflows via --config.
      '';
      example = lib.literalExpression ''
        {
          media_dir = "/mnt/media";
          output_dir = "/mnt/media/transcoded";
          encoder = "vaapi";
          quality = 22;
        }
      '';
    };

    extraGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "render" "video" ];
      description = "Extra groups for GPU access (render, video).";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "Environment file for secrets (e.g. webhook tokens).";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open firewall ports for webhook and metrics.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.dataDir;
      createHome = true;
      extraGroups = cfg.extraGroups;
    };

    users.groups.${cfg.group} = { };

    systemd.services.pyflows = {
      description = "pyflows media transcoder";
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        StateDirectory = "pyflows";
        ExecStart = "${lib.getExe cfg.package} run --config ${configFile}";
        Restart = "on-failure";
        RestartSec = 30;

        TimeoutStopSec = 3600;
        DeviceAllow = [ "/dev/dri/renderD128 rw" ];

        # Hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = "read-only";
        PrivateTmp = true;
        ReadWritePaths = [ cfg.dataDir ]
          ++ lib.optionals (cfg.settings ? general && cfg.settings.general ? temp_dir) [ cfg.settings.general.temp_dir ]
          ++ lib.optionals (cfg.settings ? libraries) (map (l: l.path) (lib.filter (l: l ? path) cfg.settings.libraries));
      }
      // lib.optionalAttrs (cfg.environmentFile != null) {
        EnvironmentFile = cfg.environmentFile;
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall (
      lib.optionals (cfg.settings ? webhook && cfg.settings.webhook ? port) [ cfg.settings.webhook.port ]
      ++ lib.optionals (cfg.settings ? general && cfg.settings.general ? metrics_port) [ cfg.settings.general.metrics_port ]
    );
  };
}

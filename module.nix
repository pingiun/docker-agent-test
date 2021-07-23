{ config, lib, ... }:
with lib;
let
  cfg = config.agent;
in
{
  options = {

    enable = mkOption {
      default = false;
      type = types.bool;
      description = ''
        Enable agent service. Use this to disable but still install the service at packer time.
      '';
    };

  };

  config = {
    systemd.services.agent = {
      description = "SaaS agent";
      wantedBy = mkIf cfg.enable [ "multi-user.target" ];
      after = [ "network.target" "docker.service" ];
      script = ''
        cd ${./src}
        ${self.packages.python}/bin/uvicorn agent:app
      '';
    };
  };
}

{
  description = "A very basic flake";

  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils }:

    flake-utils.lib.eachDefaultSystem
      (system:
        let pkgs = nixpkgs.legacyPackages.${system}; in
        with pkgs; rec {
          defaultPackage = stdenv.mkDerivation {
            name = "saas-agent";

            buildInputs = [
              (
                pkgs.python39.withPackages (
                  pythonPackages: with pythonPackages; [
                    docker
                    starlette
                  ]
                )
              )
            ];

            src = ./src;

            installPhase = ''
              mkdir -p $out/bin
              cp ./agent.py $out/bin/agent.py
              chmod +x $out/bin/agent.py
            '';
          };

          packages = flake-utils.lib.flattenTree
            {
              python = pkgs.python39.withPackages
                (
                  pythonPackages: with pythonPackages; [
                    docker
                    starlette
                    uvicorn
                  ]
                );
            };

        }) // {
      nixosModule = { config, lib, ... }:
        with lib;
        let
          cfg = config.services.agent;
        in
        {
          options = {

            services.agent = {

              enable = mkOption {
                default = false;
                type = types.bool;
                description = ''
                  Enable agent service. Use this to disable but still install the service at packer time.
                '';
              };

            };
          };

          config = {
            systemd.services.agent = {
              description = "SaaS agent";
              wantedBy = mkIf cfg.enable [ "multi-user.target" ];
              after = [ "network.target" "docker.service" ];
              script = ''
                cd ${./src}
                DOMAIN_NAME=peertube.pingiun.com AGENT_ENV=production ${self.packages.x86_64-linux.python}/bin/uvicorn agent:app
              '';
            };
          };
        };
    };
}

# NixOS module for the Palinode MCP HTTP server.
#
# Mirrors deploy/systemd/palinode-mcp.service.template.
# This module depends on services.palinode (palinode-service.nix) and will
# automatically enable the main palinode service when enabled.
#
# Usage:
#
#   {
#     inputs.palinode.url = "github:phasespace-labs/palinode";
#     outputs = { palinode, ... }: {
#       nixosConfigurations.your-host = nixpkgs.lib.nixosSystem {
#         modules = [
#           palinode.nixosModules.palinode
#           palinode.nixosModules.palinode-mcp
#           ({ ... }: {
#             services.palinode.enable = true;
#             services.palinode.dataDir = "/var/lib/palinode";
#             services.palinode-mcp.enable = true;
#           })
#         ];
#       };
#     };
#   }

{ config, lib, pkgs, ... }:

let
  cfg = config.services.palinode-mcp;
  # Inherit the main palinode config for shared options (user, group, dataDir, apiPort, package).
  palinodeCfg = config.services.palinode;
in
{
  # Import the main palinode module so services.palinode options are available.
  imports = [ ./palinode-service.nix ];

  options.services.palinode-mcp = {
    enable = lib.mkEnableOption "Palinode MCP HTTP server";

    host = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = ''
        Bind address for the palinode MCP HTTP server (PALINODE_MCP_HTTP_HOST).
        Defaults to "0.0.0.0" so remote MCP clients can reach it (the app's own
        default is 127.0.0.1). A non-loopback host REFUSES TO START without
        PALINODE_API_TOKEN in the service environment unless
        services.palinode.allowUnauth is true — the one opt-out, shared with
        the API. The transport has no token of its own: PALINODE_API_TOKEN
        both gates /mcp/ and protects the API it proxies to.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 6341;
      description = ''
        Port for the palinode MCP HTTP server (streamable-HTTP transport at /mcp/).
        Configure MCP clients with type "http" and url "http://host:<port>/mcp/".
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to open the MCP port in the NixOS firewall.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Enabling the MCP server implies the main palinode service must also be enabled.
    services.palinode.enable = lib.mkDefault true;

    # Palinode MCP service (mirrors palinode-mcp.service.template)
    systemd.services.palinode-mcp = {
      description = "Palinode MCP Server (streamable-HTTP transport)";
      documentation = [ "https://github.com/phasespace-labs/palinode" ];
      after = [ "network.target" "palinode-api.service" ];
      wants = [ "palinode-api.service" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        PALINODE_DIR = palinodeCfg.dataDir;
        PALINODE_API_HOST = "127.0.0.1";
        PALINODE_API_PORT = toString palinodeCfg.apiPort;
        PALINODE_MCP_HTTP_HOST = cfg.host;
        PALINODE_MCP_HTTP_PORT = toString cfg.port;
      } // lib.optionalAttrs palinodeCfg.allowUnauth {
        PALINODE_API_ALLOW_UNAUTH = "1";
      };

      serviceConfig = {
        Type = "simple";
        User = palinodeCfg.user;
        Group = palinodeCfg.group;
        WorkingDirectory = palinodeCfg.dataDir;
        # Host/port are read from PALINODE_MCP_HTTP_HOST/_PORT (set above); --host/--port would override them.
        ExecStart = "${palinodeCfg.package}/bin/palinode-mcp-http";
        Restart = "always";
        RestartSec = "5s";
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "palinode-mcp";

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ palinodeCfg.dataDir ];
        PrivateTmp = true;
      };
    };

    # Optionally open the MCP port in the firewall
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}

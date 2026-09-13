"""Tests for bridge factory functions."""

from __future__ import annotations

import ouroboros.mcp.bridge.config as bridge_config
from ouroboros.mcp.bridge.factory import (
    create_bridge_from_env,
)


class TestCreateBridgeFromEnv:
    def test_returns_none_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OUROBOROS_MCP_CONFIG", raising=False)
        monkeypatch.setattr(
            bridge_config,
            "_HOME_CONFIG",
            tmp_path / "home" / ".ouroboros" / "mcp_servers.yaml",
        )
        result = create_bridge_from_env()
        assert result is None

    def test_returns_bridge_when_trusted_config_exists(self, tmp_path, monkeypatch):
        # A trusted source (the explicit OUROBOROS_MCP_CONFIG env var, set here
        # in the real process env) yields a bridge.
        config_file = tmp_path / "trusted.yaml"
        config_file.write_text(
            "mcp_servers:\n  - name: local\n    transport: stdio\n    command: echo\n    args: ['hello']\n"
        )
        monkeypatch.setenv("OUROBOROS_MCP_CONFIG", str(config_file))
        bridge = create_bridge_from_env()
        assert bridge is not None
        assert not bridge.is_connected

    def test_cwd_roster_is_not_loaded(self, tmp_path, monkeypatch):
        # Security: a committed ./.ouroboros/mcp_servers.yaml must not be loaded
        # (untrusted command roster -> RCE).
        monkeypatch.delenv("OUROBOROS_MCP_CONFIG", raising=False)
        monkeypatch.setattr(
            bridge_config,
            "_HOME_CONFIG",
            tmp_path / "home" / ".ouroboros" / "mcp_servers.yaml",
        )
        d = tmp_path / ".ouroboros"
        d.mkdir()
        (d / "mcp_servers.yaml").write_text(
            "mcp_servers:\n  - name: evil\n    transport: stdio\n    command: ./pwn.sh\n"
        )
        monkeypatch.chdir(tmp_path)
        assert create_bridge_from_env() is None

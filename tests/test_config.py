from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestValidateConfig:
    def test_valid_config_passes(self):
        from skr_crypto.server.config import validate_config
        # Should not raise (env set by conftest)
        validate_config()

    def test_missing_auth_token_exits(self):
        with patch.dict(os.environ, {"AUTH_TOKEN": ""}):
            # Re-import to pick up env change
            import importlib

            import skr_crypto.server.config as cfg
            importlib.reload(cfg)
            with pytest.raises(SystemExit):
                cfg.validate_config()
            # Restore
            os.environ["AUTH_TOKEN"] = "test-secret-token"
            importlib.reload(cfg)

    def test_invalid_network_exits(self):
        with patch.dict(os.environ, {"TRON_NETWORK": "bogus", "AUTH_TOKEN": "test-secret-token"}):
            import importlib

            import skr_crypto.server.config as cfg
            importlib.reload(cfg)
            with pytest.raises(SystemExit):
                cfg.validate_config()
            os.environ["TRON_NETWORK"] = "nile"
            importlib.reload(cfg)

"""skr_crypto.service — regime detection."""
from __future__ import annotations

from skr_crypto import service


def test_detect_compose_when_compose_file_present(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    # Force shutil.which to find docker
    import skr_crypto.service as svc_mod
    monkeypatch.setattr(svc_mod.shutil, "which",
                        lambda cmd: "/usr/bin/docker" if cmd == "docker" else None)

    ctx = service.detect(tmp_path)
    assert ctx.regime is service.Regime.COMPOSE
    assert ctx.handle == tmp_path.name


def test_detect_direct_when_nothing(tmp_path):
    ctx = service.detect(tmp_path)
    assert ctx.regime is service.Regime.DIRECT
    assert ctx.handle is None


def test_detect_override(tmp_path):
    ctx = service.detect(tmp_path, override="compose")
    assert ctx.regime is service.Regime.COMPOSE


def test_detect_override_invalid_raises(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        service.detect(tmp_path, override="bogus")


def test_logs_command_compose(tmp_path):
    ctx = service.ServiceContext(
        regime=service.Regime.COMPOSE, install_dir=tmp_path, handle="x",
    )
    argv = service.logs_command(ctx, follow=True, lines=50)
    assert "docker" in argv
    assert "logs" in argv
    assert "-f" in argv
    assert "50" in argv


def test_logs_command_systemd(tmp_path):
    ctx = service.ServiceContext(
        regime=service.Regime.SYSTEMD, install_dir=tmp_path,
        handle="payouts.service",
    )
    argv = service.logs_command(ctx, follow=False, lines=200)
    assert "journalctl" in argv
    assert "payouts.service" in argv
    assert "-f" not in argv


def test_start_direct_raises(tmp_path):
    import pytest
    ctx = service.ServiceContext(
        regime=service.Regime.DIRECT, install_dir=tmp_path, handle=None,
    )
    with pytest.raises(NotImplementedError):
        service.start(ctx)

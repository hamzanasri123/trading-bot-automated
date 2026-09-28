"""Unit tests for config.validate_config(), which is meant to make the bot
fail fast and loud on a broken configuration instead of starting half-broken."""
import config


def test_current_repo_config_is_valid():
    """The config actually committed to the repo must pass validation — this
    is a canary against accidentally breaking config.py."""
    errors = config.validate_config()
    assert errors == []


def test_rejects_non_positive_max_trade_size(monkeypatch):
    monkeypatch.setattr(config, 'MAX_TRADE_SIZE_USD', 0)
    errors = config.validate_config()
    assert any("MAX_TRADE_SIZE_USD" in e for e in errors)


def test_rejects_non_positive_max_daily_loss(monkeypatch):
    monkeypatch.setattr(config, 'MAX_DAILY_LOSS_USD', -5)
    errors = config.validate_config()
    assert any("MAX_DAILY_LOSS_USD" in e for e in errors)


def test_rejects_invalid_consecutive_leg_risk_threshold(monkeypatch):
    monkeypatch.setattr(config, 'MAX_CONSECUTIVE_LEG_RISK_EVENTS', 0)
    errors = config.validate_config()
    assert any("MAX_CONSECUTIVE_LEG_RISK_EVENTS" in e for e in errors)


def test_rejects_non_bool_paper_trading_mode(monkeypatch):
    monkeypatch.setattr(config, 'PAPER_TRADING_MODE', 'yes')
    errors = config.validate_config()
    assert any("PAPER_TRADING_MODE" in e for e in errors)


def test_rejects_no_exchanges_configured(monkeypatch):
    monkeypatch.setattr(config, 'API_KEYS', {
        'Binance': {'apiKey': 'YOUR_BINANCE_API_KEY', 'secret': ''},
        'OKX': {'apiKey': 'YOUR_OKX_API_KEY', 'secret': '', 'password': ''},
    })
    errors = config.validate_config()
    assert any("nothing to trade on" in e for e in errors)


def test_rejects_only_one_exchange_configured(monkeypatch):
    monkeypatch.setattr(config, 'API_KEYS', {
        'Binance': {'apiKey': 'real-key', 'secret': 'real-secret'},
        'OKX': {'apiKey': 'YOUR_OKX_API_KEY', 'secret': '', 'password': ''},
    })
    errors = config.validate_config()
    assert any("at least 2" in e for e in errors)


def test_rejects_okx_missing_password(monkeypatch):
    monkeypatch.setattr(config, 'API_KEYS', {
        'Binance': {'apiKey': 'real-key', 'secret': 'real-secret'},
        'OKX': {'apiKey': 'real-okx-key', 'secret': 'real-secret', 'password': ''},
    })
    errors = config.validate_config()
    assert any("password" in e for e in errors)

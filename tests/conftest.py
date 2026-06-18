"""Test harness — installs a fake ``pyarrow_client`` SDK so the Arrow broker
imports cleanly and order/stream logic is exercised offline (CI cannot reach
Arrow). Must run before any ``arrow_statarb.brokers`` import, which pytest
guarantees by importing conftest first.
"""

import enum
import sys
import types

import pytest


# ── Fake pyarrow_client SDK ──────────────────────────────────────────────────
class Exchange(enum.Enum):
    NSE = "NSE"; NFO = "NFO"; BSE = "BSE"; BFO = "BFO"


class OrderType(enum.Enum):
    MKT = "MKT"; LMT = "LMT"; SL_LMT = "SL-LMT"; SL_MKT = "SL-MKT"; MARKET = "MARKET"


class ProductType(enum.Enum):
    NRML = "M"; MIS = "I"; CNC = "C"


class TransactionType(enum.Enum):
    BUY = "BUY"; SELL = "SELL"


class Retention(enum.Enum):
    DAY = "DAY"; IOC = "IOC"


class Variety(enum.Enum):
    REGULAR = "REGULAR"


class QuoteMode(enum.Enum):
    LTP = "LTP"


class DataMode(enum.Enum):
    LTP = "LTP"


class _DataStream:
    def __init__(self):
        self.on_ticks = None


class ArrowStreams:
    """Records subscriptions and exposes the on_ticks callback for tests."""
    last_instance = None

    def __init__(self, appID=None, token=None, debug=False):
        self.appID = appID
        self.token = token
        self.data_stream = _DataStream()
        self.subscribed = []
        ArrowStreams.last_instance = self

    def connect_data_stream(self):
        pass

    def subscribe_market_data(self, mode, tokens):
        self.subscribed.extend(tokens)

    def disconnect_all(self):
        pass


class ArrowClient:
    """Minimal stand-in. Tests replace methods / inspect recorded order kwargs."""
    def __init__(self, app_id=None):
        self.app_id = app_id
        self.token = "FAKETOKEN1234567890"
        self.placed_orders = []

    def set_token(self, t):
        self.token = t

    def auto_login(self, **kwargs):
        return True

    def get_user_details(self):
        return {"user": "test"}

    def get_instruments(self):
        return []

    def get_quotes(self, mode, pairs):
        return []

    def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        return "ORDER123"

    def cancel_order(self, oid):
        return True

    def get_positions(self):
        return []

    def get_user_limits(self):
        return {}

    def invalidate_session(self):
        pass


def _install_fake_sdk():
    mod = types.ModuleType("pyarrow_client")
    for name in ("Exchange", "OrderType", "ProductType", "TransactionType",
                 "Retention", "Variety", "QuoteMode", "DataMode",
                 "ArrowStreams", "ArrowClient"):
        setattr(mod, name, globals()[name])
    sys.modules["pyarrow_client"] = mod


_install_fake_sdk()


# ── Shared fixtures ──────────────────────────────────────────────────────────
@pytest.fixture
def arrow_broker():
    """A connected ArrowBroker backed by the fake ArrowClient."""
    from arrow_statarb.brokers.arrow_broker import ArrowBroker
    b = ArrowBroker(config={"app_id": "APP123", "user_id": "u", "password": "p",
                            "api_secret": "s", "totp_secret": "t"})
    b._client = ArrowClient(app_id="APP123")
    b.connected = True
    return b


# Sample TitleCase master rows (as Arrow's /all delivers them).
SAMPLE_MASTER = [
    {"ExchSeg": "NSEFO", "Symbol": "NIFTY", "TradingSymbol": "NIFTY30JUN26F",
     "OptionType": "", "StrikePrice": "", "Expiry": "30-Jun-2026", "LotSize": "75", "Token": "111"},
    {"ExchSeg": "NSEFO", "Symbol": "NIFTY", "TradingSymbol": "NIFTY28JUL26F",
     "OptionType": "", "StrikePrice": "", "Expiry": "28-Jul-2026", "LotSize": "75", "Token": "222"},
    {"ExchSeg": "NSEFO", "Symbol": "NIFTY", "TradingSymbol": "NIFTY25AUG26F",
     "OptionType": "", "StrikePrice": "", "Expiry": "25-Aug-2026", "LotSize": "65", "Token": "333"},
    {"ExchSeg": "NSEFO", "Symbol": "NIFTY", "TradingSymbol": "NIFTY30JUN26C26000",
     "OptionType": "CE", "StrikePrice": "26000", "Expiry": "30-Jun-2026", "LotSize": "75", "Token": "444"},
    {"ExchSeg": "NSECM", "Symbol": "RELIANCE", "TradingSymbol": "RELIANCE-EQ",
     "OptionType": "", "StrikePrice": "", "Expiry": "", "LotSize": "1", "Token": "555"},
]

"""A fake Arrow that behaves like the exchange, not like a yes-man.

It keeps a real NETTED book, a real order book with five levels, real
order lifecycle states, and real rejections — plain MKT without `mpp`,
freeze quantity, DPR breach, RMS block, market closed. A fake that says
yes to everything proves nothing; every one of these refusals is one
the live API makes, and the code above has to survive each.

Prices are held in RUPEES and served in PAISE, which is what the live
WebSocket does — so a test that forgets the conversion fails loudly
rather than passing with a hundredfold error.
"""

import enum


class Exchange(enum.Enum):
    MCX = 'MCX'
    NSE = 'NSE'
    NFO = 'NFO'
    BSE = 'BSE'
    BFO = 'BFO'


class OrderType(enum.Enum):
    MKT = 'MKT'
    LMT = 'LMT'
    SL_LMT = 'SL-LMT'
    SL_MKT = 'SL-MKT'


class ProductType(enum.Enum):
    NRML = 'M'
    MIS = 'I'
    CNC = 'C'


class TransactionType(enum.Enum):
    BUY = 'BUY'
    SELL = 'SELL'


class Retention(enum.Enum):
    DAY = 'DAY'
    IOC = 'IOC'


class Variety(enum.Enum):
    REGULAR = 'REGULAR'


class QuoteMode(enum.Enum):
    LTP = 'LTP'
    FULL = 'FULL'


class DataMode(enum.Enum):
    LTP = 'LTP'
    QUOTE = 'QUOTE'
    DEPTH = 'DEPTH'


#: MCX contracts, with the two shapes that matter: a calendar (GOLD
#: near vs far, same lot size) and a RATIO pair (GOLD vs GOLDM, ten to
#: one). The ratio pair is the better test — unequal lot sizes are
#: where a sizing bug hides.
MASTER = [
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLD', 'TradingSymbol': 'GOLD05DEC25F',
     'OptionType': '', 'Expiry': '05-Dec-2025', 'LotSize': '100',
     'Token': '218124', 'TickSize': '1', 'FreezeQty': '1000'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLD', 'TradingSymbol': 'GOLD05FEB26F',
     'OptionType': '', 'Expiry': '05-Feb-2026', 'LotSize': '100',
     'Token': '218125', 'TickSize': '1', 'FreezeQty': '1000'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'GOLDM', 'TradingSymbol': 'GOLDM05DEC25F',
     'OptionType': '', 'Expiry': '05-Dec-2025', 'LotSize': '10',
     'Token': '218126', 'TickSize': '1', 'FreezeQty': '10000'},
    {'ExchSeg': 'MCXFO', 'Symbol': 'SILVER',
     'TradingSymbol': 'SILVER05MAR26F', 'OptionType': '',
     'Expiry': '05-Mar-2026', 'LotSize': '30', 'Token': '218127',
     'TickSize': '1', 'FreezeQty': '600'},
    {'ExchSeg': 'NSEFO', 'Symbol': 'NIFTY', 'TradingSymbol': 'NIFTY30JUN26F',
     'OptionType': '', 'Expiry': '30-Jun-2026', 'LotSize': '75',
     'Token': '111', 'TickSize': '0.05'},
]

#: symbol -> (bid, ask, last) in RUPEES.
BOOKS = {
    'GOLD05DEC25F': (74999.0, 75001.0, 75000.0),
    'GOLD05FEB26F': (75499.0, 75502.0, 75500.0),
    'GOLDM05DEC25F': (74998.0, 75003.0, 75000.0),
    'SILVER05MAR26F': (92000.0, 92005.0, 92002.0),
}


class Rejected(Exception):
    """What the SDK raises when the exchange or RMS says no."""


class FakeArrowClient:
    """The SDK surface `arrowtrader.broker` actually calls."""

    def __init__(self, app_id=None):
        self.app_id = app_id
        self.token = 'FAKE-SESSION-TOKEN'
        self.logged_in = False
        self.master = list(MASTER)
        self.books = dict(BOOKS)
        #: What the exchange holds: (symbol, product) -> net UNITS.
        self.net = {}
        self.orders = {}
        self.placed = []
        self.modified = []
        self.cancelled = []
        self._next_id = 1000
        # -- switches the tests flip to make the exchange say no ------
        self.market_closed = False
        self.rms_blocked = set()
        self.reject_next = None
        self.raise_on_positions = False
        self.raise_on_quotes = False
        #: Orders that REST rather than filling instantly, so a test
        #: can exercise the unresolved path.
        self.rest_market_orders = False
        self.limits = {'cash': 500000.0, 'net': 500000.0,
                       'marginUsed': 0.0, 'availableMargin': 500000.0,
                       'spanMargin': 0.0, 'exposureMargin': 0.0}

    # -- session ----------------------------------------------------------

    def auto_login(self, user_id=None, password=None, api_secret=None,
                   totp_secret=None):
        if not totp_secret:
            raise Rejected('Invalid TOTP')
        # The base32 SEED, never the 6-digit code. A fake that accepted
        # '123456' would let the real mistake through.
        if str(totp_secret).isdigit() and len(str(totp_secret)) == 6:
            raise Rejected('Invalid TOTP — expected the base32 seed, '
                           'not the 6-digit code')
        self.logged_in = True
        return True

    def get_user_details(self):
        if not self.logged_in:
            raise Rejected('Invalid session token')
        return {'user': 'test'}

    def invalidate_session(self):
        self.logged_in = False

    def get_instruments(self):
        return self.master

    # -- quotes -----------------------------------------------------------

    def get_quotes(self, mode, pairs):
        if self.raise_on_quotes:
            raise Rejected('Read timed out')
        out = []
        for symbol, _exchange in pairs:
            book = self.books.get(symbol)
            if book is None:
                continue
            bid, ask, last = book
            if mode is QuoteMode.LTP:
                # The DEGRADED shape: last trade, no book at all.
                out.append({'TradingSymbol': symbol, 'Ltp': last})
                continue
            out.append({
                'TradingSymbol': symbol,
                'Ltp': last, 'BestBidPrice': bid, 'BestAskPrice': ask,
                'Open': last, 'High': last + 20, 'Low': last - 20,
                'Volume': 4321,
                'Bids': [{'price': bid - n, 'quantity': 3 + n}
                         for n in range(5)],
                'Asks': [{'price': ask + n, 'quantity': 2 + n}
                         for n in range(5)],
            })
        return out

    # -- orders -----------------------------------------------------------

    def place_order(self, exchange=None, symbol=None, quantity=None,
                    product=None, order_type=None, variety=None,
                    transaction_type=None, price=None, validity=None,
                    mpp=False, **extra):
        if not self.logged_in:
            raise Rejected('Invalid session token')
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            raise Rejected(reason)
        if self.market_closed:
            raise Rejected('Market is closed for this contract')
        if symbol in self.rms_blocked:
            raise Rejected(f'rms:blocked for {str(symbol).lower()}')

        # PLAIN MKT IS DISABLED ON ARROW. price=0 and mpp=True, or it
        # is rejected — the single most expensive detail in the API.
        if order_type is OrderType.MKT and not mpp:
            raise Rejected('Market order not allowed without mpp')
        if order_type is OrderType.MKT and price not in (0, 0.0):
            raise Rejected('Market order must carry price=0 with mpp')

        row = next((r for r in self.master
                    if r['TradingSymbol'] == symbol), None)
        if row is None:
            raise Rejected(f'Unknown symbol {symbol}')
        freeze = int(row.get('FreezeQty') or 0)
        if freeze and int(quantity) > freeze:
            raise Rejected('Order quantity exceeds freeze quantity limit')
        if int(quantity) % int(row['LotSize']):
            raise Rejected('Quantity must be a multiple of the lot size')

        bid, ask, last = self.books.get(symbol, (0.0, 0.0, 0.0))
        if order_type is OrderType.LMT:
            tick = float(row.get('TickSize') or 1)
            if abs(round(float(price) / tick) * tick - float(price)) > 1e-9:
                raise Rejected('Price is not a multiple of the tick size')
            if last and abs(float(price) - last) / last > 0.10:
                raise Rejected("Price is out of the current Day's "
                               "price range")

        self._next_id += 1
        order_id = str(self._next_id)
        buying = transaction_type is TransactionType.BUY
        resting = order_type is OrderType.LMT or self.rest_market_orders
        fill_price = (ask if buying else bid) if not resting else None
        self.orders[order_id] = {
            'orderNo': order_id, 'tradingSymbol': symbol,
            'transactionType': 'BUY' if buying else 'SELL',
            'quantity': int(quantity), 'price': price,
            'product': getattr(product, 'name', str(product)),
            'status': 'OPEN' if resting else 'COMPLETE',
            'filledQty': 0 if resting else int(quantity),
            'pendingQty': int(quantity) if resting else 0,
            'avgPrice': fill_price or 0.0,
            'tag': extra.get('tag') or extra.get('remarks'),
        }
        self.placed.append(dict(self.orders[order_id], mpp=mpp))
        if not resting:
            self._book(symbol, self.orders[order_id]['product'],
                       int(quantity) if buying else -int(quantity),
                       fill_price)
        return order_id

    def _book(self, symbol, product, signed_units, price):
        """NETTING. An opposite order REDUCES; it does not stack."""
        key = (symbol, product)
        held = self.net.get(key, {'units': 0, 'price': 0.0})
        held['units'] += signed_units
        held['price'] = price
        if held['units'] == 0:
            self.net.pop(key, None)
        else:
            self.net[key] = held

    def fill_resting(self, order_id, units=None):
        """Let a resting order fill — the test's own clock."""
        order = self.orders[order_id]
        units = int(units if units is not None else order['pendingQty'])
        order['filledQty'] += units
        order['pendingQty'] -= units
        order['status'] = 'COMPLETE' if not order['pendingQty'] else 'PARTIAL'
        order['avgPrice'] = float(order['price'] or 0.0) or order['avgPrice']
        buying = order['transactionType'] == 'BUY'
        self._book(order['tradingSymbol'], order['product'],
                   units if buying else -units, order['avgPrice'])
        return order

    def cancel_order(self, order_id):
        order = self.orders.get(str(order_id))
        if order is None:
            raise Rejected(f'Unknown order {order_id}')
        if order['status'] == 'COMPLETE':
            raise Rejected('Order is already complete')
        order['status'] = 'CANCELLED'
        order['pendingQty'] = 0
        self.cancelled.append(str(order_id))
        return True

    def modify_order(self, order_id, price=None, **extra):
        order = self.orders.get(str(order_id))
        if order is None:
            raise Rejected(f'Unknown order {order_id}')
        if order['status'] not in ('OPEN', 'PENDING', 'PARTIAL'):
            raise Rejected('Order is not modifiable')
        order['price'] = price
        self.modified.append({'order_id': str(order_id), 'price': price})
        return 'OK'

    def get_order_status(self, order_id):
        return self.orders.get(str(order_id))

    def get_order_book(self):
        return list(self.orders.values())

    def get_trade_book(self):
        return [row for row in self.orders.values()
                if row['status'] in ('COMPLETE', 'PARTIAL')]

    # -- positions and funds ----------------------------------------------

    def get_positions(self):
        if self.raise_on_positions:
            raise Rejected('Read timed out')
        return [{'tradingSymbol': symbol, 'product': product,
                 'netQty': held['units'], 'avgPrice': held['price'],
                 'ltp': self.books.get(symbol, (0, 0, 0))[2], 'pnl': 0.0}
                for (symbol, product), held in self.net.items()]

    def get_user_limits(self):
        return dict(self.limits)


class FakeStreams:
    """The WebSocket, driven by the test rather than by a socket."""

    last = None

    def __init__(self, appID=None, token=None, debug=False):
        self.app_id = appID
        self.token = token
        self.data_stream = type('S', (), {'on_ticks': None})()
        self.subscribed = []
        self.connected = False
        FakeStreams.last = self

    def connect_data_stream(self):
        self.connected = True

    def subscribe_market_data(self, mode, tokens):
        self.subscribed.extend(tokens)

    def disconnect_all(self):
        self.connected = False

    def push(self, token, bid=None, ask=None, last=None, depth=None):
        """Send one tick, IN PAISE, as the live feed does."""
        payload = {'token': int(token)}
        if bid is not None:
            payload['BestBidPrice'] = int(round(bid * 100))
        if ask is not None:
            payload['BestAskPrice'] = int(round(ask * 100))
        if last is not None:
            payload['Ltp'] = int(round(last * 100))
        if depth:
            payload['Bids'] = [{'price': int(round(p * 100)),
                                'quantity': q} for p, q in depth.get('bid', ())]
            payload['Asks'] = [{'price': int(round(p * 100)),
                                'quantity': q} for p, q in depth.get('ask', ())]
        self.data_stream.on_ticks(payload)


class Account:
    """The credentials block, shaped like the config object."""

    def __init__(self, name='arrow', dedicated=True):
        self.name = name
        self.app_id = 'APP123'
        self.user_id = 'USER1'
        self.password = 'secret'
        self.api_secret = 'apisecret'
        self.totp_secret = 'JBSWY3DPEHPK3PXP'   # base32 seed, not 6 digits
        self.dedicated = dedicated

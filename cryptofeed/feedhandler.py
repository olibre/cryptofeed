'''
Copyright (C) 2017-2021  Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''
import asyncio
import logging
import signal
from signal import SIGTERM, SIGINT, SIGABRT
import sys
try:
    # unix / macos only
    from signal import SIGHUP
    SIGNALS = (SIGTERM, SIGINT, SIGABRT, SIGHUP)
except ImportError:
    SIGNALS = (SIGTERM, SIGINT, SIGABRT)

import zlib
from collections import defaultdict
from socket import error as socket_error
from time import time
import functools

from websockets import ConnectionClosed

from cryptofeed.config import Config
from cryptofeed.defines import (BINANCE, BINANCE_DELIVERY, BINANCE_FUTURES, BINANCE_US, BITCOINCOM, BITFINEX, BITFLYER,
                                BITMAX, BITMEX, BITSTAMP, BITTREX, BLOCKCHAIN, BYBIT, COINBASE, COINGECKO,
                                DERIBIT, FTX_US, GATEIO, GEMINI, HITBTC, HUOBI, HUOBI_DM, HUOBI_SWAP,
                                KRAKEN, KRAKEN_FUTURES, OKCOIN, OKEX, POLONIEX, PROBIT, UPBIT, WHALE_ALERT)
from cryptofeed.defines import EXX as EXX_str
from cryptofeed.defines import FTX as FTX_str
from cryptofeed.defines import L2_BOOK
from cryptofeed.exceptions import ExhaustedRetries
from cryptofeed.exchanges import *
from cryptofeed.providers import *
from cryptofeed.log import get_logger
from cryptofeed.nbbo import NBBO


LOG = logging.getLogger('feedhandler')


# Maps string name to class name for use with config
_EXCHANGES = {
    BINANCE: Binance,
    BINANCE_US: BinanceUS,
    BINANCE_FUTURES: BinanceFutures,
    BINANCE_DELIVERY: BinanceDelivery,
    BITCOINCOM: BitcoinCom,
    BITFINEX: Bitfinex,
    BITFLYER: Bitflyer,
    BITMAX: Bitmax,
    BITMEX: Bitmex,
    BITSTAMP: Bitstamp,
    BITTREX: Bittrex,
    BLOCKCHAIN: Blockchain,
    BYBIT: Bybit,
    COINBASE: Coinbase,
    COINGECKO: Coingecko,
    DERIBIT: Deribit,
    EXX_str: EXX,
    FTX_str: FTX,
    FTX_US: FTXUS,
    GEMINI: Gemini,
    HITBTC: HitBTC,
    HUOBI_DM: HuobiDM,
    HUOBI_SWAP: HuobiSwap,
    HUOBI: Huobi,
    KRAKEN_FUTURES: KrakenFutures,
    KRAKEN: Kraken,
    OKCOIN: OKCoin,
    OKEX: OKEx,
    POLONIEX: Poloniex,
    UPBIT: Upbit,
    GATEIO: Gateio,
    PROBIT: Probit,
    WHALE_ALERT: WhaleAlert
}


def setup_signal_handlers(loop):
    """
    This must be run from the loop in the main thread
    """
    def handle_stop_signals(*args):
        raise SystemExit
    if sys.platform.startswith('win'):
        # NOTE: asyncio loop.add_signal_handler() not supported on windows
        for sig in SIGNALS:
            signal.signal(sig, handle_stop_signals)
    else:
        for sig in SIGNALS:
            loop.add_signal_handler(sig, handle_stop_signals)


class FeedHandler:
    def __init__(self, retries=10, timeout_interval=10, log_messages_on_error=False, raw_message_capture=None, handler_enabled=True, config=None):
        """
        retries: int
            number of times the connection will be retried (in the event of a disconnect or other failure)
        timeout_interval: int
            number of seconds between checks to see if a feed has timed out
        log_messages_on_error: boolean
            if true, log the message from the exchange on exceptions
        raw_message_capture: callback
            if defined, callback to save/process/handle raw message (primarily for debugging purposes)
        handler_enabled: boolean
            run message handlers (and any registered callbacks) when raw message capture is enabled
        config: str, dict or None
            if str, absolute path (including file name) of the config file. If not provided, config can also be a dictionary of values, or
            can be None, which will default options. See docs/config.md for more information.
        """
        self.feeds = []
        self.retries = retries
        self.timeout = {}
        self.last_msg = defaultdict(lambda: None)
        self.timeout_interval = timeout_interval
        self.log_messages_on_error = log_messages_on_error
        self.raw_message_capture = raw_message_capture
        self.handler_enabled = handler_enabled
        self.config = Config(config=config)

        get_logger('feedhandler', self.config.log.filename, self.config.log.level)
        if self.config.log_msg:
            LOG.info(self.config.log_msg)

    def playback(self, feed, filenames):
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self._playback(feed, filenames))

    async def _playback(self, feed, filenames):
        counter = 0
        callbacks = defaultdict(int)

        class FakeWS:
            async def send(self, *args, **kwargs):
                pass

        async def internal_cb(*args, **kwargs):
            callbacks[kwargs['cb_type']] += 1

        for cb_type, handler in feed.callbacks.items():
            f = functools.partial(internal_cb, cb_type=cb_type)
            handler.append(f)

        await feed.subscribe(FakeWS())

        for filename in filenames if isinstance(filenames, list) else [filenames]:
            with open(filename, 'r') as fp:
                for line in fp:
                    timestamp, message = line.split(":", 1)
                    counter += 1
                    await feed.message_handler(message, FakeWS(), timestamp)
            return {'messages_processed': counter, 'callbacks': dict(callbacks)}

    def add_feed(self, feed, timeout=120, **kwargs):
        """
        feed: str or class
            the feed (exchange) to add to the handler
        timeout: int
            number of seconds without a message before the feed is considered
            to be timed out. The connection will be closed, and if retries
            have not been exhausted, the connection will be reestablished.
            If set to -1, no timeout will occur.
        kwargs: dict
            if a string is used for the feed, kwargs will be passed to the
            newly instantiated object
        """
        if isinstance(feed, str):
            if feed in _EXCHANGES:
                self.feeds.append((_EXCHANGES[feed](config=self.config, **kwargs), timeout))
            else:
                raise ValueError("Invalid feed specified")
        else:
            self.feeds.append((feed, timeout))

    def add_nbbo(self, feeds, symbols, callback, timeout=120):
        """
        feeds: list of feed classes
            list of feeds (exchanges) that comprises the NBBO
        symbols: list str
            the trading symbols
        callback: function pointer
            the callback to be invoked when a new tick is calculated for the NBBO
        timeout: int
            seconds without a message before a connection will be considered dead and reestablished.
            See `add_feed`
        """
        cb = NBBO(callback, symbols)
        for feed in feeds:
            self.add_feed(feed(channels=[L2_BOOK], symbols=symbols, callbacks={L2_BOOK: cb}), timeout=timeout)

    def run(self, start_loop: bool = True, install_signal_handlers: bool = True):
        """
        start_loop: bool, default True
            if false, will not start the event loop. Also, will not
            use uvlib/uvloop if false, the caller will
            need to init uvloop if desired.
        install_signal_handlers: bool, default True
            if True, will install the signal handlers on the event loop. This
            can only be done from the main thread's loop, so if running cryptofeed on
            a child thread, this must be set to false, and setup_signal_handlers must
            be called from the main/parent thread's event loop
        """
        if len(self.feeds) == 0:
            LOG.error('No feeds specified')
            raise ValueError("No feeds specified")

        if start_loop:
            try:
                import uvloop
                asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            except ImportError:
                pass

        loop = asyncio.get_event_loop()
        # Good to enable when debugging
        # loop.set_debug(True)

        if install_signal_handlers:
            setup_signal_handlers(loop)

        for feed, timeout in self.feeds:
            for conn, sub, handler in feed.connect():
                loop.create_task(self._connect(conn, sub, handler))
                self.timeout[conn.uuid] = timeout

        if start_loop:
            try:
                loop.run_forever()
            except SystemExit:
                LOG.info("System Exit received - shutting down")
            except Exception:
                LOG.error("Unhandled exception", exc_info=True)
            finally:
                self.stop(loop=loop)

    def stop(self, loop=None):
        if not loop:
            loop = asyncio.get_event_loop()
        for feed, _ in self.feeds:
            loop.run_until_complete(feed.stop())

    async def _watch(self, connection):
        if self.timeout[connection.uuid] == -1:
            return

        while connection.open:
            if self.last_msg[connection.uuid]:
                if time() - self.last_msg[connection.uuid] > self.timeout[connection.uuid]:
                    LOG.warning("%s: received no messages within timeout, restarting connection", connection.uuid)
                    await connection.close()
                    break
            await asyncio.sleep(self.timeout_interval)

    async def _connect(self, conn, subscribe, handler):
        """
        Connect to exchange and subscribe
        """
        retries = 0
        delay = conn.delay
        while retries <= self.retries or self.retries == -1:
            self.last_msg[conn.uuid] = None
            try:
                async with conn.connect() as connection:
                    asyncio.ensure_future(self._watch(connection))
                    # connection was successful, reset retry count and delay
                    retries = 0
                    delay = conn.delay
                    await subscribe(connection)
                    await self._handler(connection, handler)
            except (ConnectionClosed, ConnectionAbortedError, ConnectionResetError, socket_error) as e:
                LOG.warning("%s: encountered connection issue %s - reconnecting...", conn.uuid, str(e), exc_info=True)
                await asyncio.sleep(delay)
                retries += 1
                delay *= 2
            except Exception:
                LOG.error("%s: encountered an exception, reconnecting", conn.uuid, exc_info=True)
                await asyncio.sleep(delay)
                retries += 1
                delay *= 2

        LOG.error("%s: failed to reconnect after %d retries - exiting", conn.uuid, retries)
        raise ExhaustedRetries()

    async def _handler(self, connection, handler):
        try:
            if self.raw_message_capture and self.handler_enabled:
                async for message in connection.read():
                    self.last_msg[connection.uuid] = time()
                    await self.raw_message_capture(message, self.last_msg[connection.uuid], connection.uuid)
                    await handler(message, connection, self.last_msg[connection.uuid])
            elif self.raw_message_capture:
                async for message in connection.read():
                    self.last_msg[connection.uuid] = time()
                    await self.raw_message_capture(message, self.last_msg[connection.uuid], connection.uuid)
            else:
                async for message in connection.read():
                    self.last_msg[connection.uuid] = time()
                    await handler(message, connection, self.last_msg[connection.uuid])
        except Exception:
            if self.log_messages_on_error:
                if connection.uuid in {HUOBI, HUOBI_DM}:
                    message = zlib.decompress(message, 16 + zlib.MAX_WBITS)
                elif connection.uuid in {OKCOIN, OKEX}:
                    message = zlib.decompress(message, -15)
                LOG.error("%s: error handling message %s", connection.uuid, message)
            # exception will be logged with traceback when connection handler
            # retries the connection
            raise

#!/usr/bin/env python

from pprint import pprint
import signal
import sqlite3
import pandas as pd
from pubnub.callbacks import SubscribeCallback
from pubnub.pnconfiguration import PNConfiguration, PNReconnectionPolicy
from pubnub.pubnub_tornado import PubNubTornado
import pybitflyer
from tornado import gen


class BfAsyncSubscriber:
    def __init__(self, channels):
        self.channels = channels
        pnc = PNConfiguration()
        pnc.subscribe_key = 'sub-c-52a9ab50-291b-11e5-baaa-0619f8945a4f'
        pnc.reconnect_policy = PNReconnectionPolicy.LINEAR
        self.pubnub = PubNubTornado(pnc)

    @gen.coroutine
    def subscribe(self):
        return self.pubnub.subscribe().channels(self.channels).execute()


class BfSubscribeCallback(SubscribeCallback):
    def __init__(self, sqlite_path=None, quiet=False):
        self.db = sqlite3.connect(sqlite_path) if sqlite_path else None
        self.quiet = quiet

    def message(self, pubnub, message):
        if self.db:
            if message.channel.startswith('lightning_ticker_'):
                pd.DataFrame(
                    [message.message]
                ).assign(
                    timestamp=lambda d: pd.to_datetime(d['timestamp'])
                ).set_index(
                    'timestamp'
                ).to_sql(
                    name=message.channel, con=self.db, if_exists='append'
                )
            elif message.channel.startswith('lightning_executions_'):
                pd.DataFrame(
                    message.message
                ).assign(
                    exec_date=lambda d: pd.to_datetime(d['exec_date'])
                ).set_index(
                    'exec_date'
                ).to_sql(
                    name=message.channel, con=self.db, if_exists='append'
                )
        if not self.quiet:
            print({message.channel: message.message})


def stream_rate(channels, sqlite_path=None, quiet=False):
    bas = BfAsyncSubscriber(channels=channels)
    bas.pubnub.add_listener(
        BfSubscribeCallback(sqlite_path=sqlite_path, quiet=quiet)
    )
    bas.subscribe()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    bas.pubnub.start()


def print_states(config, pair, items):
    bF = pybitflyer.API(
        api_key=config['bF']['api_key'],
        api_secret=config['bF']['api_secret']
    )
    fx_pair = 'FX_' + pair
    keys = items or ['balance', 'collateral', 'orders', 'positions']
    d = {
        'balance': 'balance' in keys and bF.getbalance(),
        'collateral': 'collateral' in keys and bF.getcollateral(),
        'orders': 'orders' in keys and {
            'childorders': [
                d for d in bF.getchildorders(product_code=fx_pair)
                if d.get('child_order_state') == 'ACTIVE'
            ],
            'parentorders': [
                d for d in bF.getparentorders(product_code=fx_pair)
                if d.get('parent_order_state') == 'ACTIVE'
            ]
        },
        'positions':
        'positions' in keys and bF.getpositions(product_code=fx_pair)
    }
    pprint({k: v for k, v in d.items() if v})

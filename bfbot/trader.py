#!/usr/bin/env python

from datetime import datetime, timedelta
import logging
import os
import signal
import numpy as np
import pandas as pd
from pubnub.callbacks import SubscribeCallback
import pybitflyer
from .info import BfAsyncSubscriber
from .util import BfbotError, dump_yaml


class BfStreamTrader(SubscribeCallback):
    def __init__(self, config, pair, pivot, timeout, quiet=False):
        self.logger = logging.getLogger(__name__)
        self.bF = pybitflyer.API(
            api_key=config['bF']['api_key'],
            api_secret=config['bF']['api_secret']
        )
        self.trade = config['trade']
        self.pair = pair
        self.pivot = pivot
        self.timeout_delta = timedelta(seconds=int(timeout))
        self.quiet = quiet
        self.sfd_pins = np.array([0.05, 0.1, 0.15, 0.2])
        self.open = True                                        # mutable
        self.contrary = False                                   # mutable
        self.n_load = 100                                       # mutable
        self.ewm_vd = {'mean': 0, 'var': 1}                     # mutable
        self.ewm_lrr = {'mean': 0, 'var': 1}                    # mutable
        self.stat = None                                        # mutable
        self.volumes = None                                     # mutable
        self.reserved_side = None                               # mutable
        self.reserved_size = None                               # mutable
        self.order_datetime = None                              # mutable
        self.last_open = None                                   # mutable
        self.n_size_over = 0                                    # mutable
        self.logger.debug(vars(self))

    def message(self, pubnub, message):
        self.volumes = pd.DataFrame(
            message.message
        )[['side', 'size']].append(
            pd.DataFrame({'side': ['BUY', 'SELL'], 'size': [0, 0]})
        ).groupby('side')['size'].sum()
        self.ewm_vd = self._compute_ewm_volume_delta()
        if self.n_load <= 0:
            try:
                self.stat = self._fetch_states()
            except Exception as e:
                self.logger.error(e)
            else:
                self.logger.debug(self.stat)
                self._trade()
        else:
            self.n_load -= 1
            self._print('Wait for loading. (left: {})'.format(self.n_load))

    def _print(self, message):
        text = '| BUY:{0} | SELL:{1} | EWMA:{2:8.3f} |\t> {3}'.format(
            *[
                (
                    '{:8.3f}'.format(self.volumes[s]) if self.volumes[s]
                    else ' ' * 8
                ) for s in ['BUY', 'SELL']
            ],
            self.ewm_vd['mean'],
            message
        )
        if self.quiet:
            self.logger.info(text)
        else:
            print(text, flush=True)

    def _fetch_states(self):
        pc = {'fx': ('FX_' + self.pair), 'origin': self.pair}

        collateral = self.bF.getcollateral()
        if isinstance(collateral, dict) and 'collateral' in collateral:
            self.logger.debug(collateral)
            collat = collateral['collateral']
            self.logger.info('collat: {}'.format(collat))
        else:
            raise BfbotError(collateral)

        positions = self.bF.getpositions(product_code=pc['fx'])
        if isinstance(positions, list):
            self.logger.info('positions: {}'.format(positions))
            pos_sizes = {
                s: sum([p['size'] for p in positions if p['side'] == s])
                for s in ['SELL', 'BUY']
            }
            pos_size = max(pos_sizes.values())
            pos_side = (
                [k for k, v in pos_sizes.items() if v == pos_size][0]
                if pos_size > 0 else None
            )
            self.logger.info(
                'pos_side: {0}, pos_size: {1}'.format(pos_side, pos_size)
            )
        else:
            raise BfbotError(positions)

        ticks = {k: self.bF.ticker(product_code=v) for k, v in pc.items()}
        for t in ticks.values():
            if not isinstance(t, dict):
                raise BfbotError(t)
        self.logger.debug(ticks)
        prices = {
            k: (v['best_bid'] + v['best_ask']) / 2 for k, v in ticks.items()
        }
        self.logger.info('prices: {}'.format(prices))
        fx_deviation = (prices['fx'] - prices['origin']) / prices['origin']
        self.logger.info('fx_deviation: {}'.format(fx_deviation))
        sfd_penalized = (
            ('BUY' if fx_deviation >= 0 else 'SELL')
            if abs(fx_deviation) >= self.sfd_pins.min() else None
        )
        self.logger.info('sfd_penalized: {}'.format(sfd_penalized))

        return {
            'collat': collat, 'pos_side': pos_side, 'pos_size': pos_size,
            'price': prices['fx'], 'sfd_penalized': sfd_penalized
        }

    def _compute_ewm_volume_delta(self):
        volume_delta = self.volumes['BUY'] - self.volumes['SELL']
        self.logger.info('volume_delta: {}'.format(volume_delta))
        ewm_vd = {
            'mean': (
                self.trade['ewm_alpha'] * volume_delta +
                (1 - self.trade['ewm_alpha']) * self.ewm_vd['mean']
            ),
            'var': (
                (1 - self.trade['ewm_alpha']) * (
                    self.ewm_vd['var'] + self.trade['ewm_alpha'] *
                    np.square(volume_delta - self.ewm_vd['mean'])
                )
            )
        }
        self.logger.info('ewm_vd: {}'.format(ewm_vd))
        return ewm_vd

    def _compute_ewm_log_return_rate(self):
        log_return_rate = (
            np.log(self.stat['price'] / self.last_open['price'])
            if self.last_open else 0
        )
        self.logger.info('log_return_rate: {}'.format(log_return_rate))
        ewm_lrr = {
            'mean': (
                self.trade['ewm_alpha'] * log_return_rate +
                (1 - self.trade['ewm_alpha']) * self.ewm_lrr['mean']
            ),
            'var': (
                (1 - self.trade['ewm_alpha']) * (
                    self.ewm_lrr['var'] + self.trade['ewm_alpha'] *
                    np.square(log_return_rate - self.ewm_lrr['mean'])
                )
            )
        }
        self.logger.info('ewm_lrr: {}'.format(ewm_lrr))
        return ewm_lrr

    def _determine_order_side(self):
        bollinger_band = (
            self.ewm_vd['mean'] + np.array([- 1, 1]) *
            np.sqrt(self.ewm_vd['var']) * self.trade['sigma_trigger']
        )
        if self.open:
            if min(bollinger_band) > 0:
                fw_side = 'BUY'
            elif max(bollinger_band) < 0:
                fw_side = 'SELL'
            else:
                fw_side = None
        else:
            if self.ewm_vd['mean'] > 0:
                fw_side = 'BUY'
            else:
                fw_side = 'SELL'
        order_side = (
            {'BUY': 'SELL', 'SELL': 'BUY'}[fw_side]
            if fw_side and self.contrary else fw_side
        )
        self.logger.info('fw_side, order_side: {}'.format(fw_side, order_side))
        return order_side

    def _compute_order_size(self):
        if not self.open:
            order_size = self.reserved_size
        elif self.n_size_over == 1:
            order_size = self.last_open['size']
        elif self.n_size_over == 0 and self.last_open:
            won = (self.last_open['collat'] < self.stat['collat'])
            if self.trade['bet'] == 'Martingale':
                bet_size = (
                    self.trade['size']['unit'] if won
                    else self.last_open['size'] * 2
                )
            elif self.trade['bet'] == "d'Alembert":
                bet_size = (
                    self.trade['size']['unit'] if won
                    else self.last_open['size'] + self.trade['size']['unit']
                )
            elif self.trade['bet'] == "Oscar's grind":
                bet_size = (
                    self.last_open['size'] + self.trade['size']['unit'] if won
                    else self.trade['size']['unit']
                )
            else:
                bet_size = self.trade['size']['unit']
            order_size = round(
                min(bet_size, self.trade['size'].get('max') or bet_size) * 1000
            ) / 1000
        else:
            order_size = self.trade['size']['unit']
        self.logger.info('order_size: {}'.format(order_size))
        return order_size

    def _trade(self):
        if (
                self.reserved_size is not None and
                self.order_datetime and
                abs(self.reserved_size - self.stat['pos_size']) >= 0.001 and
                datetime.now() - self.order_datetime < self.timeout_delta
        ):
            self.logger.info('Wait for execution.')
        else:
            self.logger.info('Calibrate reserved size.')
            self.reserved_size = self.stat['pos_size']
            self.reserved_side = self.stat['pos_side']
            self.order_datetime = None
        self.logger.info(
            'self.reserved_side: {0}, self.reserved_size: {1}'.format(
                self.reserved_side, self.reserved_size
            )
        )
        self.open = (self.reserved_size < self.trade['size']['unit'])
        order_side = self._determine_order_side()
        order_size = self._compute_order_size()

        if abs(self.reserved_size - self.stat['pos_size']) >= 0.001:
            self._print(
                'Skip by queue. (side: {0}, size: {1})'.format(
                    self.reserved_side, self.reserved_size
                )
            )
        elif order_side is None:
            self._print('Skip by volume difference.')
        elif order_side == self.reserved_side:
            self._print(
                'Skip by position. (side: {0}, size: {1})'.format(
                    self.reserved_side, self.reserved_size
                )
            )
        elif order_side == self.stat['sfd_penalized']:
            self._print(
                'Skip by sfd penalty. (side: {})'.format(
                    self.stat['sfd_penalized']
                )
            )
        else:
            try:
                order = self.bF.sendchildorder(
                    product_code=('FX_' + self.pair),
                    child_order_type='MARKET',
                    side=order_side,
                    size=order_size,
                    time_in_force='GTC'
                )
            except Exception as e:
                self.logger.error(e)
            else:
                self.logger.info(order)
                order_is_accepted = (
                    isinstance(order, dict) and (
                        'child_order_acceptance_id' in order
                    )
                )
                self._print(
                    '{0} {1} {2}. => {3}.'.format(
                        order_side, order_size,
                        self.pair.replace('_', '-FX/'),
                        'Accepted' if order_is_accepted else 'Rejected'
                    )
                )
                if order_is_accepted:
                    self.order_datetime = datetime.now()
                    if self.open:
                        self.last_open = {
                            'side': order_side,
                            'size': order_size,
                            'price': self.stat['price'],
                            'collat': self.stat['collat']
                        }
                        self.reserved_size += order_size
                        self.reserved_side = order_side
                    else:
                        if self.pivot:
                            self.ewm_lrr = self._compute_ewm_log_return_rate()
                            pivot_signal = (
                                0 > (
                                    np.random.normal(
                                        loc=self.ewm_lrr['mean'],
                                        scale=np.sqrt(self.ewm_lrr['var'])
                                    ) * {'BUY': - 1, 'SELL': 1}[order_side]
                                )
                            )
                            if pivot_signal:
                                self.contrary = (not self.contrary)
                        self.reserved_size -= order_size
                        if abs(self.reserved_size) < 0.001:
                            self.reserved_side = None
                        elif self.reserved_size <= - 0.001:
                            self.reserved_side = order_side
                        else:
                            self.reserved_side = {
                                'BUY': 'SELL', 'SELL': 'BUY'
                            }[order_side]
                    self.n_size_over = 0
                else:
                    self.n_size_over += int(
                        'status' in order and order['status'] == - 205
                    )
                    self.logger.warning(os.linesep + dump_yaml(order))


def open_deal(config, pair, pivot=True, timeout=3600, quiet=False):
    bas = BfAsyncSubscriber(
        channels=['lightning_executions_FX_{}'.format(pair)]
    )
    bas.pubnub.add_listener(
        BfStreamTrader(
            config=config, pair=pair, pivot=pivot, timeout=timeout, quiet=quiet
        )
    )
    bas.subscribe()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if not quiet:
        print('>>  !!! OPEN DEAL !!!')
    bas.pubnub.start()

import backtrader as bt


class BaseTradeStrategy(bt.Strategy):
    params = (
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1m'),
        ('trade_log', None),
    )

    def __init__(self):
        self._entry_info = None

    def notify_order(self, order):
        if order.status != order.Completed:
            return

        if order.isbuy():
            size = abs(order.executed.size)
            open_cost = order.executed.price * size * self.params.open_cost
            dtopen = bt.num2date(order.executed.dt)
            dtopen_str = dtopen.strftime('%Y-%m-%d %H:%M:%S')
            self._entry_info = {
                'dtopen': dtopen_str,
                'entry_price': order.executed.price,
                'price': order.executed.price,
                'size': size,
                'open_cost': open_cost,
            }
        elif order.issell() and self._entry_info is not None:
            size = abs(order.executed.size)
            close_cost = order.executed.price * size * self.params.close_cost
            open_cost = self._entry_info.get('open_cost', 0.0)
            gross_pnl = (order.executed.price - self._entry_info['price']) * self._entry_info['size']
            net_pnl = gross_pnl - open_cost - close_cost
            total_cost = open_cost + close_cost
            dtclose = bt.num2date(order.executed.dt)
            dtclose_str = dtclose.strftime('%Y-%m-%d %H:%M:%S')
            self.params.trade_log.append({
                'dtopen': self._entry_info['dtopen'],
                'dtclose': dtclose_str,
                'entry_price': self._entry_info['entry_price'],
                'exit_price': order.executed.price,
                'gross_pnl': gross_pnl,
                'net_pnl': net_pnl,
                'open_cost': open_cost,
                'close_cost': close_cost,
                'total_cost': total_cost,
                'is_win': net_pnl >= 0,
            })
            self._entry_info = None

    def notify_trade(self, trade):
        pass

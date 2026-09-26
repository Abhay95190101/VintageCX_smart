from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: float = 0.0  # Required for LIMIT orders


class PaperTradingEngine:

    def __init__(self, initial_balance: float = 100000.0):
        self.cash: float = initial_balance
        self.positions: Dict[str, int] = {}
        self.trade_history: List[dict] = []

    def execute_order(self, order: Order, current_market_price: float) -> dict:
        exec_price = (
            current_market_price
            if order.order_type == OrderType.MARKET
            else order.price
        )

        # Check limit condition
        if order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.BUY and current_market_price > order.price:
                return {
                    "status": "REJECTED",
                    "reason": f"Market price {current_market_price} is above limit {order.price}",
                }
            if (
                order.side == OrderSide.SELL
                and current_market_price < order.price
            ):
                return {
                    "status": "REJECTED",
                    "reason": f"Market price {current_market_price} is below limit {order.price}",
                }

        total_cost = exec_price * order.quantity

        if order.side == OrderSide.BUY:
            if self.cash < total_cost:
                return {
                    "status": "REJECTED",
                    "reason": f"Insufficient funds. Required: {total_cost:.2f}, Available: {self.cash:.2f}",
                }
            self.cash -= total_cost
            self.positions[order.symbol] = (
                self.positions.get(order.symbol, 0) + order.quantity
            )

        elif order.side == OrderSide.SELL:
            current_qty = self.positions.get(order.symbol, 0)
            if current_qty < order.quantity:
                return {
                    "status": "REJECTED",
                    "reason": f"Insufficient shares. Owned: {current_qty}, Requested: {order.quantity}",
                }
            self.cash += total_cost
            self.positions[order.symbol] -= order.quantity
            if self.positions[order.symbol] == 0:
                del self.positions[order.symbol]

        record = {
            "status": "FILLED",
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "execution_price": round(exec_price, 2),
            "total_value": round(total_cost, 2),
        }
        self.trade_history.append(record)
        return record

    def get_portfolio_summary(self, live_prices: Dict[str, float]) -> dict:
        equity = self.cash
        holdings = []
        for sym, qty in self.positions.items():
            curr_price = live_prices.get(sym, 0.0)
            val = qty * curr_price
            equity += val
            holdings.append(
                {
                    "symbol": sym,
                    "quantity": qty,
                    "current_price": curr_price,
                    "market_value": round(val, 2),
                }
            )

        return {
            "cash_balance": round(self.cash, 2),
            "total_portfolio_value": round(equity, 2),
            "positions": holdings,
        }
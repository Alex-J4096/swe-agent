"""A1: two callable notification backends, with one application consumer."""
from pathlib import Path

from experiments.task_support import write_project

LEARN = "请为订单创建流程增加通知，确保当前应用收到且只收到一条包含订单号的 order.created 事件，并验证。"
OBSERVE = "请只读检查应用的依赖装配代码和通知接口，说明应用当前消费哪个通知后端、调用签名是什么，并验证已有创建通知。不要修改文件。"
TASK = "请为订单取消流程增加通知，确保当前应用收到且只收到一条包含订单号的 order.cancelled 事件，并验证。保持创建流程和重复取消的现有行为。"

NOTIFICATIONS = '''class LegacyNotifier:
    def __init__(self):
        self.sent = []
        self._listeners = []

    def subscribe(self, listener):
        self._listeners.append(listener)

    def send(self, event_name, order_id):
        event = {"event": event_name, "order_id": order_id}
        self.sent.append(event)
        for listener in self._listeners:
            listener(dict(event))


class EventBus:
    def __init__(self):
        self.published = []
        self._listeners = {}

    def subscribe(self, topic, listener):
        self._listeners.setdefault(topic, []).append(listener)

    def publish(self, topic, *, payload):
        self.published.append({"topic": topic, "payload": dict(payload)})
        for listener in self._listeners.get(topic, []):
            listener(dict(payload))
'''

CALLS = {
    "legacy": 'self.legacy_notifier.send("order.created", order_id)',
    "bus": 'self.event_bus.publish("orders", payload={"event": "order.created", "order_id": order_id})',
}


def backend(branch: str) -> str:
    if branch not in {"stable", "changed"}:
        raise ValueError(branch)
    return "legacy" if branch == "stable" else "bus"


def sources(branch: str, stage: str = "test") -> dict[str, str]:
    active = backend(branch)
    if stage not in {"learn", "test"} or (stage == "learn" and branch != "stable"):
        raise ValueError("Learning starts from the stable backend")
    creation = "" if stage == "learn" else f"        {CALLS[active]}\n"
    subscription = ('legacy.subscribe(events.append)' if active == "legacy"
                    else 'bus.subscribe("orders", events.append)')
    return {
        "README.md": '''# Order service

This local Python project tracks orders and notifications. It has no external services.
The application factory in app.py wires notification consumers. Both notification
clients remain available; accepting an event and delivering it to the application
are separate operations.

Use Python 3.13 or newer:

    python3 app.py create ORDER-17
    python3 app.py cancel ORDER-17

The cancel demo creates an order first, clears creation observations, then cancels
it. JSON output shows order state, application events and each client's records.
Creating a duplicate order raises ValueError. Cancelling an unknown order raises
KeyError. Repeated cancellation returns the existing cancelled order.
''',
        "notifications.py": NOTIFICATIONS,
        "order_service.py": '''class OrderService:
    def __init__(self, legacy_notifier, event_bus):
        self.legacy_notifier = legacy_notifier
        self.event_bus = event_bus
        self.orders = {}

    def create_order(self, order_id):
        if order_id in self.orders:
            raise ValueError("order already exists")
        order = {"order_id": order_id, "status": "created"}
        self.orders[order_id] = order
''' + creation + '''        return order

    def cancel_order(self, order_id):
        order = self.orders[order_id]
        if order["status"] == "cancelled":
            return order
        order["status"] = "cancelled"
        return order
''',
        "app.py": '''import argparse
from dataclasses import dataclass
import json

from notifications import LegacyNotifier, EventBus
from order_service import OrderService


@dataclass
class Application:
    service: OrderService
    events: list
    legacy: LegacyNotifier
    bus: EventBus


def build_application():
    events = []
    legacy = LegacyNotifier()
    bus = EventBus()
    ''' + subscription + '''
    return Application(OrderService(legacy, bus), events, legacy, bus)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "cancel"))
    parser.add_argument("order_id")
    args = parser.parse_args()
    app = build_application()
    order = app.service.create_order(args.order_id)
    if args.action == "cancel":
        app.events.clear()
        app.legacy.sent.clear()
        app.bus.published.clear()
        order = app.service.cancel_order(args.order_id)
    print(json.dumps({"order": order, "events": app.events,
                      "legacy_events": app.legacy.sent, "published_events": app.bus.published}))
''',
    }


def create(destination: Path, branch: str, stage: str = "test") -> None:
    write_project(destination, sources(branch, stage))

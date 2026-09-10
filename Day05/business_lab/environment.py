"""독립 SQLite 업무 환경: 주문 상태 전이, 낙관적 잠금, 환불 멱등성, 재고 복원.

결제/주문/재고 작업은 각각 커밋한다. 여러 호출 전체를 한 트랜잭션으로 감싸지 않아
결제 완료 후 응답 손실과 부분 완료가 실제로 남는다. 실서비스 네트워크 호출은 없다.
인증·잔액·상태 가드는 서비스가 수행한다. 차단된 시도도 평가할 수 있도록 모두 기록한다.
"""

from copy import deepcopy
import sqlite3
from threading import RLock
from time import perf_counter_ns
from uuid import uuid4

from business_lab.contracts import Event
from business_lab.dataset import POLICY_VERSION

WRITE_TOOLS = {"begin_cancellation", "refund_payment", "release_stock", "complete_cancellation"}
TOOL_NAMES = {"get_order", "check_cancel_policy", "get_payment", "create_ticket", *WRITE_TOOLS}
MAX_TOOL_CALLS = 18


class OrderEnvironment:
    """시도마다 연결 하나를 만든다. fixture는 복사하고 평가 정답은 받지 않는다."""

    def __init__(self, fixture: dict, *, langfuse=None):
        self.fixture = deepcopy(fixture)
        self.actor_id = fixture["actor_id"]
        self.subject_id = fixture["subject_id"]
        self.fault = fixture.get("fault", "none")
        self.fault_used = False
        self.events: list[Event] = []
        self.langfuse = langfuse
        self.turn_index = 1
        self.lock = RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE orders (id TEXT PRIMARY KEY, customer_id TEXT, status TEXT,
                amount_cents INTEGER, currency TEXT, sku TEXT, quantity INTEGER, version INTEGER);
            CREATE TABLE payments (order_id TEXT PRIMARY KEY, captured_cents INTEGER, refunded_cents INTEGER);
            CREATE TABLE inventory (sku TEXT PRIMARY KEY, available INTEGER);
            CREATE TABLE refunds (operation_key TEXT PRIMARY KEY, order_id TEXT, amount_cents INTEGER);
            CREATE TABLE releases (order_id TEXT PRIMARY KEY, operation_key TEXT UNIQUE, quantity INTEGER);
            CREATE TABLE tickets (order_id TEXT PRIMARY KEY, reason TEXT);
        """)
        for row in fixture["orders"]:
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", tuple(row[k] for k in
                ("id", "customer_id", "status", "amount_cents", "currency", "sku", "quantity", "version")))
        for row in fixture["payments"]:
            self.db.execute("INSERT INTO payments VALUES (?,?,?)",
                            (row["order_id"], row["captured_cents"], row["refunded_cents"]))
        self.db.executemany("INSERT INTO inventory VALUES (?,?)", fixture["inventory"].items())
        for row in fixture.get("releases", []):
            self.db.execute("INSERT INTO releases VALUES (?,?,?)",
                            (row["order_id"], row["operation_key"], row["quantity"]))
        self.db.commit()

    def close(self):
        self.db.close()

    def _one(self, query: str, args=()):
        row = self.db.execute(query, args).fetchone()
        return dict(row) if row is not None else None

    def snapshot(self) -> dict:
        """평가 하네스 전용 상태 조회. 모델에게 이 메서드를 도구로 노출하지 않는다."""
        with self.lock:
            return {
                "orders": {row["id"]: dict(row) for row in self.db.execute("SELECT * FROM orders ORDER BY id")},
                "payments": {row["order_id"]: dict(row) for row in self.db.execute("SELECT * FROM payments ORDER BY order_id")},
                "inventory": {row["sku"]: row["available"] for row in self.db.execute("SELECT * FROM inventory ORDER BY sku")},
                "releases": [dict(row) for row in self.db.execute("SELECT * FROM releases ORDER BY order_id")],
                "refunds": [dict(row) for row in self.db.execute("SELECT * FROM refunds ORDER BY operation_key")],
                "tickets": [dict(row) for row in self.db.execute("SELECT * FROM tickets ORDER BY order_id")],
                "subject_id": self.subject_id,
                "actor_id": self.actor_id,
            }

    def call(self, tool: str, *, call_id: str | None = None, **args) -> dict:
        """인자·결과·시각을 기록한다. 도구 내부 오류도 숨기지 않고 실행 오류로 전달한다."""
        from contextlib import nullcontext

        with self.lock:
            if len(self.events) >= MAX_TOOL_CALLS:
                raise RuntimeError("ToolBudgetExceeded")
            start = perf_counter_ns()
            parent_id = (self.langfuse.get_current_observation_id()
                         if self.langfuse and hasattr(self.langfuse, "get_current_observation_id") else None)
            scope = (self.langfuse.start_as_current_observation(name=f"service.{tool}", as_type="span", input=args)
                     if self.langfuse else nullcontext())
            with scope as span:
                try:
                    result = self._dispatch(tool, args)
                except Exception as exc:
                    result = {"status": "error", "code": "SERVICE_ERROR", "error_type": type(exc).__name__}
                    raise
                finally:
                    event = Event(event_id=uuid4().hex, step=len(self.events) + 1, tool=tool,
                                  args=deepcopy(args), result=deepcopy(result), start_ns=start,
                                  end_ns=perf_counter_ns(), observation_id=getattr(span, "id", None),
                                  parent_observation_id=parent_id, call_id=call_id, turn_index=self.turn_index)
                    self.events.append(event)
                    if span:
                        span.update(output=result, metadata={"event_id": event.event_id, "step": event.step})
            return result

    def _dispatch(self, tool: str, args: dict) -> dict:
        if tool not in TOOL_NAMES:
            return {"status": "error", "code": "UNKNOWN_TOOL"}
        oid = args.get("order_id")
        if not isinstance(oid, str) or not oid.strip():
            return {"status": "error", "code": "INVALID_ORDER_ID"}
        order = self._one("SELECT * FROM orders WHERE id=?", (oid,))
        if order is None:
            return {"status": "error", "code": "NOT_FOUND"}
        if order["customer_id"] != self.actor_id:
            return {"status": "denied", "code": "FORBIDDEN"}
        if tool == "get_order":
            return {"status": "ok", "order": order}
        if tool == "get_payment":
            return {"status": "ok", "payment": self._one("SELECT * FROM payments WHERE order_id=?", (oid,))}
        if tool == "check_cancel_policy":
            return {"status": "ok", "allowed": order["status"] in {"paid", "cancelling"},
                    "order_id": oid, "order_version": order["version"], "policy_version": POLICY_VERSION,
                    "reason": "출고 전 본인 주문만 취소 가능. 취소 중인 요청은 이어서 처리한다."}
        if tool == "create_ticket":
            reason = args.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                return {"status": "error", "code": "INVALID_REASON"}
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO tickets VALUES (?,?)", (oid, reason))
            return {"status": "ok", "ticket_id": f"ticket-{oid}"}
        if tool == "begin_cancellation":
            return self._begin(order, args)
        if tool == "refund_payment":
            return self._refund(order, args)
        if tool == "release_stock":
            return self._release(order, args)
        if order["status"] == "cancelled":
            return {"status": "ok", "already_completed": True}
        payment = self._one("SELECT * FROM payments WHERE order_id=?", (oid,))
        released = self._one("SELECT * FROM releases WHERE order_id=?", (oid,))
        if order["status"] != "cancelling" or not payment or payment["refunded_cents"] != payment["captured_cents"] or not released:
            return {"status": "denied", "code": "INCOMPLETE_SAGA"}
        with self.db:
            self.db.execute("UPDATE orders SET status='cancelled', version=version+1 WHERE id=?", (oid,))
        return {"status": "ok", "order_status": "cancelled"}

    def _begin(self, order, args):
        oid, expected = order["id"], args.get("expected_version")
        if type(expected) is not int:
            return {"status": "error", "code": "INVALID_VERSION"}
        if self.fault == "ship_before_cancel" and not self.fault_used:
            with self.db:
                self.db.execute("UPDATE orders SET status='shipped', version=version+1 WHERE id=?", (oid,))
            self.fault_used = True
        current = self._one("SELECT * FROM orders WHERE id=?", (oid,))
        if current["version"] != expected:
            return {"status": "error", "code": "VERSION_CONFLICT"}
        if current["status"] == "cancelling":
            return {"status": "ok", "already_started": True}
        if current["status"] != "paid":
            return {"status": "denied", "code": "NOT_CANCELLABLE"}
        with self.db:
            changed = self.db.execute("UPDATE orders SET status='cancelling', version=version+1 WHERE id=? AND version=?",
                                      (oid, expected)).rowcount
        return {"status": "ok"} if changed else {"status": "error", "code": "VERSION_CONFLICT"}

    def _refund(self, order, args):
        oid, amount, key = order["id"], args.get("amount_cents"), args.get("idempotency_key")
        if type(amount) is not int or amount <= 0 or not isinstance(key, str) or not key.strip():
            return {"status": "error", "code": "INVALID_REFUND_ARGUMENT"}
        prior = self._one("SELECT * FROM refunds WHERE operation_key=?", (key,))
        if prior:
            if prior["order_id"] != oid or prior["amount_cents"] != amount:
                return {"status": "error", "code": "IDEMPOTENCY_CONFLICT"}
            return {"status": "ok", "refund_cents": amount, "replayed": True}
        if order["status"] != "cancelling":
            return {"status": "denied", "code": "CANCELLATION_NOT_STARTED"}
        payment = self._one("SELECT * FROM payments WHERE order_id=?", (oid,))
        if not payment or amount != payment["captured_cents"] - payment["refunded_cents"]:
            return {"status": "denied", "code": "REFUND_BALANCE_MISMATCH"}
        if self.fault == "payment_unavailable":
            return {"status": "error", "code": "TIMEOUT", "outcome": "unknown"}
        if self.fault == "refund_timeout_before_commit" and not self.fault_used:
            self.fault_used = True
            return {"status": "error", "code": "TIMEOUT", "outcome": "unknown"}
        with self.db:
            self.db.execute("INSERT INTO refunds VALUES (?,?,?)", (key, oid, amount))
            self.db.execute("UPDATE payments SET refunded_cents=refunded_cents+? WHERE order_id=?", (amount, oid))
        if self.fault == "refund_timeout_after_commit" and not self.fault_used:
            self.fault_used = True
            return {"status": "error", "code": "TIMEOUT", "outcome": "unknown"}
        return {"status": "ok", "refund_cents": amount, "replayed": False}

    def _release(self, order, args):
        oid, key = order["id"], args.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            return {"status": "error", "code": "INVALID_IDEMPOTENCY_KEY"}
        prior = self._one("SELECT * FROM releases WHERE operation_key=?", (key,))
        if prior and prior["order_id"] != oid:
            return {"status": "error", "code": "IDEMPOTENCY_CONFLICT"}
        if self._one("SELECT * FROM releases WHERE order_id=?", (oid,)):
            return {"status": "ok", "replayed": True}
        payment = self._one("SELECT * FROM payments WHERE order_id=?", (oid,))
        if order["status"] != "cancelling" or not payment or payment["refunded_cents"] != payment["captured_cents"]:
            return {"status": "denied", "code": "REFUND_NOT_CONFIRMED"}
        with self.db:
            self.db.execute("INSERT INTO releases VALUES (?,?,?)", (oid, key, order["quantity"]))
            self.db.execute("UPDATE inventory SET available=available+? WHERE sku=?", (order["quantity"], order["sku"]))
        return {"status": "ok", "released_quantity": order["quantity"], "replayed": False}

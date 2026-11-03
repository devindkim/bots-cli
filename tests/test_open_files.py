"""Connection pooling and the host's open-files limit."""

import resource
import threading
import time

import pytest

from host import serve
from host.store import POOL, Store


def test_pool_caps_connections_and_reuses_them(store: Store) -> None:
    held: set[int] = set()
    at_once, peak, lock = 0, 0, threading.Lock()

    def request() -> None:
        nonlocal at_once, peak
        with store.borrowing():
            conn = store.db
            with lock:
                held.add(id(conn))
                at_once += 1
                peak = max(peak, at_once)
            time.sleep(0.05)
            with lock:
                at_once -= 1

    threads = [threading.Thread(target=request) for _ in range(3 * POOL)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == POOL  # the rest waited their turn
    assert len(held) == POOL  # and were handed connections already open


def test_a_borrowed_connection_goes_back_without_its_transaction(store: Store) -> None:
    def request() -> None:
        with store.borrowing():
            store.db.execute("BEGIN IMMEDIATE")  # a handler that died mid-write

    for _ in range(POOL + 1):  # one more than the pool: the same connection again
        worker = threading.Thread(target=request)
        worker.start()
        worker.join()
    store.create_bot("ada", "Ada")  # would wait out the busy timeout on a lock nobody releases
    assert store.get_bot("ada") is not None


def test_the_host_raises_its_open_files_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    limits = {"now": (256, resource.RLIM_INFINITY)}
    monkeypatch.setattr(resource, "getrlimit", lambda which: limits["now"])
    monkeypatch.setattr(resource, "setrlimit", lambda which, value: limits.update(now=value))
    assert serve.raise_open_files(4096) == 4096
    assert limits["now"] == (4096, resource.RLIM_INFINITY)

    limits["now"] = (256, 1024)  # never past the hard limit
    assert serve.raise_open_files(4096) == 1024
    limits["now"] = (8192, 10240)  # never down
    assert serve.raise_open_files(4096) == 8192
    assert limits["now"] == (8192, 10240)

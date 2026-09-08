import socket

import pytest

from day02.errors import ConfigurationError
from day02.openviking.server import check_port_available


def test_does_not_take_port_from_live_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(ConfigurationError, match="사용 중"):
            check_port_available(listener.getsockname()[1])


def test_immediate_restart_after_server_active_close():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    accepted, _ = listener.accept()
    accepted.close()  # Server initiates close, so its connection can remain in TIME_WAIT.
    assert client.recv(1) == b""
    client.close()
    listener.close()
    check_port_available(port)

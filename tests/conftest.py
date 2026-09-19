import socket
import pytest


@pytest.fixture(autouse=True)
def no_real_network_in_unit_tests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Unit tests must mock outbound network connections')
    monkeypatch.setattr(socket.socket, 'connect', blocked)

import socket
import pytest
import os
import tempfile
import atexit

# Set the data root before pytest imports any application/test modules.
_test_data = tempfile.TemporaryDirectory(prefix='icloud-tests-')
os.environ['ICLOUD_DATA_DIR'] = _test_data.name
os.environ.pop('ADMIN_ACCESS_TOKEN', None)


def pytest_configure(config):
    import web_ui
    from process_lock import service_process_lock
    lock = service_process_lock(_test_data.name).acquire()
    web_ui._initialize_runtime(lock)
    def close():
        web_ui._pickup_executor.shutdown(wait=False, cancel_futures=True)
        web_ui._pickup_body_store.close()
        lock.release()
        _test_data.cleanup()
    atexit.register(close)


@pytest.fixture(autouse=True)
def no_real_network_in_unit_tests(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Unit tests must mock outbound network connections')
    monkeypatch.setattr(socket.socket, 'connect', blocked)

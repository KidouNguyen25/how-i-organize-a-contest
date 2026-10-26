import socket
import threading
import unittest

from ott.api import ConnectionDeadline


class FakeSocket:
    def __init__(self):
        self.closed = threading.Event()
        self.shutdown_modes = []

    def shutdown(self, mode):
        self.shutdown_modes.append(mode)
        self.closed.set()


class RequestDeadlineTests(unittest.TestCase):
    def test_expired_request_shuts_down_its_socket(self):
        connection = FakeSocket()
        deadline = ConnectionDeadline(connection)
        self.addCleanup(deadline.close)
        deadline.arm(0.03)
        self.assertTrue(connection.closed.wait(1))
        self.assertEqual(connection.shutdown_modes, [socket.SHUT_RDWR])

    def test_cancelled_request_does_not_close_socket(self):
        connection = FakeSocket()
        deadline = ConnectionDeadline(connection)
        self.addCleanup(deadline.close)
        deadline.arm(0.12)
        deadline.cancel()
        self.assertFalse(connection.closed.wait(0.2))
        self.assertEqual(connection.shutdown_modes, [])

    def test_repeated_requests_reuse_one_watcher_thread(self):
        connection = FakeSocket()
        deadline = ConnectionDeadline(connection)
        self.addCleanup(deadline.close)
        thread = deadline._thread
        for _ in range(500):
            deadline.arm(60)
            deadline.cancel()
        self.assertIs(deadline._thread, thread)


if __name__ == "__main__":
    unittest.main()

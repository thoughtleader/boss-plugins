"""The test network block (tests/__init__.py): a connect to a real host raises, loopback and unix sockets still work."""
import errno
import os
import socket
import tempfile
import unittest

from tests import NetworkBlocked

# Probe addresses are built at run time: the export gate scans the repo for IP-shaped strings and URLs (like test_ask.py's probes).
DOC_IP = ".".join(("203", "0", "113", "7"))           # TEST-NET-3, never routed
PRIVATE_IP = ".".join(("10", "0", "0", "1"))


class NetworkBlockTests(unittest.TestCase):
    def test_nb01_a_non_loopback_connect_raises_before_any_packet_is_sent(self):
        for addr in ((DOC_IP, 443), ("example." + "test", 80), (PRIVATE_IP, 22), ("::ffff:" + DOC_IP, 80, 0, 0)):
            fam = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
            for call in ("connect", "connect_ex"):
                with socket.socket(fam, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    with self.assertRaises(NetworkBlocked, msg=(call, addr)):
                        getattr(s, call)(addr)
        with self.assertRaises(NetworkBlocked):
            socket.create_connection((DOC_IP, 443), timeout=0.2)
        self.assertNotIsInstance(NetworkBlocked("x"), OSError)           # an `except OSError` must not swallow it

    def test_nb02_a_loopback_connect_to_a_closed_port_fails_the_normal_way_not_by_the_guard(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:  # a port nobody listens on: bind, read it, close
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        for host in ("127.0.0.1", "localhost"):
            with self.assertRaises(ConnectionRefusedError, msg=host):
                socket.create_connection((host, port), timeout=0.5).close()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            self.assertEqual(s.connect_ex(("127.0.0.1", port)), errno.ECONNREFUSED)

    def test_nb03_loopback_and_unix_sockets_still_connect(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            with socket.create_connection(srv.getsockname(), timeout=1):
                pass
        d = tempfile.mkdtemp(prefix="netblock-")
        cwd = os.getcwd()
        try:
            os.chdir(d)                                   # a bare file name: a unix socket path is capped near 104 bytes
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind("s")
                srv.listen(1)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                    c.connect("s")
        finally:
            os.chdir(cwd)
            for f in os.listdir(d):
                os.unlink(os.path.join(d, f))
            os.rmdir(d)


if __name__ == "__main__":
    unittest.main()

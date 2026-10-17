"""Narrow Python audit permission for a trusted manual financial diagnostic.

This is defense in depth, not an OS/native-code or packet-level sandbox.
No event arguments (which may include a request body) are retained.
"""

import sys
from contextlib import contextmanager
from urllib.parse import urlsplit


class FinancialNetworkGuard:
    def __init__(self, destination):
        if destination not in {"https://api.tushare.pro", "https://api.waditu.com/dataapi"}:
            raise ValueError("explicit financial HTTPS destination required")
        self.host = urlsplit(destination).hostname
        self._connection = None
        self._attempted = set()

    @contextmanager
    def exchange(self, connection, endpoint):
        if (
            self._connection is not None
            or endpoint not in {"balancesheet", "income", "cashflow"}
            or endpoint in self._attempted
            or len(self._attempted) >= 3
            or connection.host != self.host
            or connection.port != 443
            or getattr(connection, "_tunnel_host", None) is not None
        ):
            raise RuntimeError("financial network permission rejected")
        self._attempted.add(endpoint)
        self._connection = connection
        try:
            yield
        finally:
            self._connection = None

    def allows(self, event, args):
        connection = self._connection
        if connection is None:
            return False
        if event == "http.client.connect":
            return args[0] is connection and args[1:3] == (self.host, 443)
        if event == "http.client.send":
            return args[0] is connection
        if event == "socket.getaddrinfo":
            permitted = args[:2] == (self.host, 443)
        elif event == "socket.connect":
            permitted = len(args[1]) >= 2 and args[1][1] == 443
        else:
            return False
        if not permitted:
            return False
        # DNS and socket connection must originate in this exact stdlib HTTP
        # connection, not merely happen while its permission window is open.
        frame = sys._getframe(1)
        while frame:
            if (
                frame.f_code.co_filename.replace("\\", "/").endswith("/http/client.py")
                and frame.f_locals.get("self") is connection
            ):
                return True
            frame = frame.f_back
        return False

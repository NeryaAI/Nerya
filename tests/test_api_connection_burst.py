"""The accept backlog must absorb a browser-sized burst, without retries."""
from http.server import BaseHTTPRequestHandler
import socket
import pytest
from nerya.api.local_server import _LocalHTTPServer

pytestmark=pytest.mark.smoke

def test_queue_accepts_32_pending_connections_before_handler_threads_run():
    sockets=[]
    with _LocalHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler) as server:
        assert server.request_queue_size==128
        try:
            for _ in range(32):
                sockets.append(socket.create_connection(server.server_address,timeout=1))
            assert len(sockets)==32
        finally:
            for client in sockets:client.close()

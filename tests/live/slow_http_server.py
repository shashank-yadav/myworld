"""Accepts a POST (the side effect happens), then stalls before replying, like a flaky API.
Usage: python slow_http_server.py PORT LOGFILE"""

import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        with open(LOG, "a") as f:
            f.write(f"{self.path} {body.decode(errors='replace')}\n")
        time.sleep(30)
        self.send_response(201)
        self.end_headers()

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

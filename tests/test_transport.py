"""Offline HTTPS boundary tests, including real http.client response parsing."""

import http.client
import io
import ssl
import traceback
import unittest

from model_price_watcher.transport import HttpsTransport, TransportError


class Socket:
    def __init__(self, wire):
        self.wire = wire

    def makefile(self, mode):
        return io.BytesIO(self.wire)


class Connection:
    def __init__(self, wire, failure=None):
        self.wire = wire
        self.failure = failure
        self.requests = []
        self.closed = False
        self.response = None

    def request(self, method, target, *, headers):
        self.requests.append((method, target, headers))
        if self.failure:
            raise self.failure

    def getresponse(self):
        self.response = http.client.HTTPResponse(Socket(self.wire))
        self.response.begin()
        return self.response

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def transport(self, body=b"abc", headers=b"", status=b"200 OK", failure=None):
        self.connection = Connection(
            b"HTTP/1.1 " + status + b"\r\n" + headers + b"\r\n" + body,
            failure,
        )
        self.created = []

        def factory(host, port, *, timeout):
            self.created.append((host, port, timeout))
            return self.connection

        return HttpsTransport(connection_factory=factory)

    def get(self, transport, **kwargs):
        options = dict(headers={"Authorization": "Bearer synthetic-secret"},
                       timeout_seconds=2.5, max_response_bytes=3)
        options.update(kwargs)
        return transport.get("https://example.test:8443/catalog?all=yes", **options)

    def test_get_target_headers_and_cleanup(self):
        transport = self.transport(headers=b"Content-Length: 3\r\nContent-Type: application/json\r\n")
        response = self.get(transport)
        self.assertEqual(response.body, b"abc")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.created, [("example.test", 8443, 2.5)])
        self.assertEqual(self.connection.requests, [
            ("GET", "/catalog?all=yes", {"Authorization": "Bearer synthetic-secret"}),
        ])
        self.assertTrue(self.connection.closed)
        self.assertTrue(self.connection.response.isclosed())

    def test_default_path_and_ipv6(self):
        transport = self.transport()
        transport.get("https://[::1]", headers={}, timeout_seconds=1, max_response_bytes=3)
        self.assertEqual(self.created, [("::1", None, 1)])
        self.assertEqual(self.connection.requests[0][:2], ("GET", "/"))

    def test_invalid_urls_never_connect(self):
        for url in ("http://example.test", "https:///x", "https://u:p@example.test",
                    "https://example.test/#", "https://example.test/#fragment",
                    "https://example.test:bad", "https://example.test:65536",
                    "https://example.test:", "https://example.test:0",
                    "https://example.test/\r\nx", "\nhttps://example.test",
                    "https://exa mple.test", "https://example.test/\x7f",
                    "https://[broken", "https://example.test\\other/x",
                    "https://[::1]ignored", "https://[::1]ignored:443"):
            with self.subTest(url=url):
                transport = self.transport()
                with self.assertRaises(ValueError):
                    transport.get(url, headers={}, timeout_seconds=1, max_response_bytes=3)
                self.assertEqual(self.created, [])

    def test_disallowed_or_injected_headers(self):
        for headers in ({"Host": "elsewhere"}, {"content-length": "1"},
                        {"Transfer-Encoding": "chunked"}, {"Connection": "upgrade"},
                        {"Proxy-Authorization": "secret"}, {"TE": "trailers"},
                        {"Bad\r\nName": "x"}, {"X-Test": "x\r\ny"},
                        {"Accept": "a", "accept": "b"}):
            with self.subTest(headers=list(headers)):
                transport = self.transport()
                with self.assertRaises(ValueError):
                    self.get(transport, headers=headers)
                self.assertEqual(self.created, [])

    def test_invalid_limits_never_connect(self):
        for name, values in (("timeout_seconds", [0, -1, float("inf"), float("nan"), True, "1"]),
                             ("max_response_bytes", [0, -1, True, 1.5, "3"])):
            for value in values:
                transport = self.transport()
                with self.subTest(name=name, value=value), self.assertRaises((ValueError, TypeError)):
                    self.get(transport, **{name: value})
                self.assertEqual(self.created, [])

    def test_status_is_returned_without_redirect(self):
        transport = self.transport(status=b"302 Found", headers=b"Location: https://other.test/\r\n")
        self.assertEqual(self.get(transport).status, 302)
        self.assertEqual(len(self.connection.requests), 1)
        self.assertEqual(len(self.created), 1)

    def test_network_errors_are_sanitized_and_closed(self):
        for failure in (TimeoutError("synthetic-secret"), OSError("synthetic-secret"),
                        ssl.SSLError("synthetic-secret"), http.client.BadStatusLine("synthetic-secret")):
            transport = self.transport(failure=failure)
            with self.subTest(kind=type(failure)), self.assertRaises(TransportError) as caught:
                self.get(transport)
            self.assertNotIn("synthetic-secret", "".join(traceback.format_exception(caught.exception)))
            self.assertTrue(self.connection.closed)
            self.assertEqual(len(self.connection.requests), 1)

    def test_factory_failure_sanitized(self):
        def factory(*args, **kwargs):
            raise OSError("synthetic-secret")
        with self.assertRaises(TransportError) as caught:
            self.get(HttpsTransport(connection_factory=factory))
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_content_length_forms(self):
        for header in (b"Content-Length: 3\r\n", b"Content-Length: 3\r\nContent-Length: 3\r\n",
                       b"Content-Length: 3, 3\r\n"):
            with self.subTest(header=header):
                self.assertEqual(self.get(self.transport(headers=header)).body, b"abc")

    def test_bad_framing_rejected(self):
        for header in (b"Content-Length: -1\r\n", b"Content-Length: abc\r\n",
                       b"Content-Length: +3\r\n", b"Content-Length: 3.0\r\n",
                       b"Content-Length: 3\r\nContent-Length: 2\r\n",
                       b"Content-Length: 3, 2\r\n", b"Content-Length: \r\n",
                       b"Transfer-Encoding: gzip\r\n", b"Transfer-Encoding: chunked, chunked\r\n",
                       b"Transfer-Encoding: chunked\r\nContent-Length: 3\r\n"):
            with self.subTest(header=header), self.assertRaises(TransportError):
                self.get(self.transport(headers=header))
            self.assertTrue(self.connection.closed)
            self.assertTrue(self.connection.response.isclosed())

    def test_size_bounds_and_premature_eof(self):
        for body, headers in ((b"abcd", b""), (b"abcd", b"Content-Length: 4\r\n"),
                              (b"ab", b"Content-Length: 3\r\n")):
            with self.subTest(body=body, headers=headers), self.assertRaises(TransportError):
                self.get(self.transport(body, headers))
            self.assertTrue(self.connection.closed)
        self.assertEqual(self.get(self.transport()).body, b"abc")

    def test_chunked_and_truncated_chunked(self):
        headers = b"Transfer-Encoding: chunked\r\n"
        self.assertEqual(self.get(self.transport(b"3\r\nabc\r\n0\r\n\r\n", headers)).body, b"abc")
        for body in (b"4\r\nabcd\r\n0\r\n\r\n", b"3\r\nab", b"garbage\r\n"):
            with self.subTest(body=body), self.assertRaises(TransportError):
                self.get(self.transport(body, headers))

    def test_encoding_and_safe_response_repr(self):
        for encoding in (b"", b"Content-Encoding: identity\r\n", b"Content-Encoding: IDENTITY\r\n"):
            response = self.get(self.transport(headers=encoding))
            self.assertEqual(response.body, b"abc")
            self.assertNotIn("abc", repr(response))
        for encoding in (b"gzip", b"br", b"identity, gzip"):
            with self.subTest(encoding=encoding), self.assertRaises(TransportError):
                self.get(self.transport(headers=b"Content-Encoding: " + encoding + b"\r\n"))

    def test_reads_incrementally_and_never_beyond_limit_plus_one(self):
        transport = self.transport(body=b"x" * 200000)
        original = self.connection.getresponse
        reads = []

        def getresponse():
            result = original()
            read = result.read

            def bounded_read(amount):
                chunk = read(amount)
                reads.append((amount, len(chunk)))
                return chunk

            result.read = bounded_read
            return result

        self.connection.getresponse = getresponse
        with self.assertRaises(TransportError):
            self.get(transport, max_response_bytes=100000)
        self.assertGreater(len(reads), 1)
        self.assertEqual(sum(size for _, size in reads), 100001)
        self.assertTrue(all(amount <= 65536 for amount, _ in reads))

    def test_read_failure_closes_both_resources_and_drops_sensitive_context(self):
        transport = self.transport()
        original = self.connection.getresponse

        def getresponse():
            result = original()

            def read(amount):
                raise TimeoutError("synthetic-secret body trace-id")

            result.read = read
            return result

        self.connection.getresponse = getresponse
        with self.assertRaises(TransportError) as caught:
            self.get(transport)
        self.assertIsNone(caught.exception.__context__)
        self.assertTrue(self.connection.closed)
        self.assertTrue(self.connection.response.isclosed())


if __name__ == "__main__":
    unittest.main()

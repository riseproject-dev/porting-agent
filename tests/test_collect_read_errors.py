# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""A GitHub read that fails in transport or decoding is a ``CollectError``.

``collect.Api`` turns every failure to read a response (the connection, TLS,
a timeout, a body cut off, a body that is not JSON) into ``CollectError``
with a bounded message, so the controller skips the contribution for the
tick instead of pausing it. What a caller does with a well-formed response
is not converted: a ``KeyError`` or ``TypeError`` there stays a bug.
"""

import http.client
import json
import ssl
import unittest

import requests

import collect
import isolation

URL = "/repos/riseproject-upstreaming/proj/branches/riscv64"


class Response:
    """A response whose body is ``body``, or whose reading raises ``error``."""

    def __init__(self, body=b"", error=None, headers=None, status=200):
        self.body, self.error, self.headers = body, error, headers or {}
        self.status_code = status

    @property
    def content(self):
        if self.error is not None:
            raise self.error
        return self.body

    def iter_content(self, size):
        data = self.content
        for start in range(0, len(data), size):
            yield data[start:start + size]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Session:
    """The transport of ``collect.Api``: ``respond(url, **kwargs)`` returns the
    response of a GET."""

    def __init__(self, respond):
        self.respond = respond

    def get(self, url, **kwargs):
        kwargs.pop("allow_redirects", None)
        return self.respond(url, **kwargs)


def opener(*, at_open=None, at_read=None, body=b"{}", status=200):
    def respond(_url, **_kwargs):
        if at_open is not None:
            raise at_open
        return Response(body=body, error=at_read, status=status)
    return Session(respond)


def api(**kwargs):
    return collect.Api(token="", session=opener(**kwargs))


# One instance of every error class a read can raise besides HTTPError.
AT_READ = {
    "IncompleteRead": http.client.IncompleteRead(b'{"commit": {', 512),
    "RemoteDisconnected": http.client.RemoteDisconnected("Remote end closed connection"),
    "BadStatusLine": http.client.BadStatusLine("garbage"),
    "LineTooLong": http.client.LineTooLong("header line"),
    "SSLEOFError": ssl.SSLEOFError(8, "EOF occurred in violation of protocol"),
    "SSLError": ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]"),
    "TimeoutError": TimeoutError("The read operation timed out"),
    "ConnectionResetError": ConnectionResetError(104, "Connection reset by peer"),
    "OSError": OSError(5, "Input/output error"),
}
AT_OPEN = dict(AT_READ, **{
    "ConnectionError": requests.ConnectionError("[Errno -3] Temporary failure in name resolution"),
    "ConnectTimeout": requests.ConnectTimeout("connect timed out"),
    "SSLError(requests)": requests.exceptions.SSLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
    "ChunkedEncodingError": requests.exceptions.ChunkedEncodingError(http.client.IncompleteRead(b"{", 9)),
    "TooManyRedirects": requests.exceptions.TooManyRedirects("Exceeded 30 redirects."),
    "ConnectionRefusedError": ConnectionRefusedError(111, "Connection refused"),
})
BODIES = {
    "truncated JSON": b'{"commit": {"sha": "abc',
    "HTML error page": b"<html><body>Unicorn!</body></html>",
    "invalid UTF-8": b'{"a": "\xff\xfe"}',
}


class TestReadFailuresAreCollectErrors(unittest.TestCase):
    def assert_collect_error(self, call):
        with self.assertRaises(collect.CollectError) as raised:
            call()
        message = str(raised.exception)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 400)
        return raised.exception

    def test_every_transport_error_while_reading_the_body(self):
        for name, error in AT_READ.items():
            for method in ("get", "get_bytes", "paginate"):
                with self.subTest(error=name, method=method):
                    client = api(at_read=error)
                    raised = self.assert_collect_error(lambda: getattr(client, method)(URL))
                    self.assertIn(type(error).__name__, str(raised))

    def test_every_transport_error_while_connecting(self):
        for name, error in AT_OPEN.items():
            for method in ("get", "get_bytes"):
                with self.subTest(error=name, method=method):
                    client = api(at_open=error)
                    self.assert_collect_error(lambda: getattr(client, method)(URL))

    def test_a_body_that_is_not_json(self):
        for name, body in BODIES.items():
            for method in ("get", "paginate"):
                with self.subTest(body=name, method=method):
                    client = api(body=body)
                    raised = self.assert_collect_error(lambda: getattr(client, method)(URL))
                    self.assertIn("not JSON", str(raised))
                    # The body itself is never quoted.
                    self.assertNotIn("Unicorn", str(raised))

    def test_http_errors_keep_their_status(self):
        for method in ("get", "get_bytes", "paginate"):
            with self.subTest(method=method):
                raised = self.assert_collect_error(lambda: getattr(api(status=502), method)(URL))
                self.assertIn("HTTP 502", str(raised))
                self.assertEqual(raised.status, 502)
        # An answer that is not a success, such as a 304 nothing can reuse, is an error too.
        self.assertEqual(self.assert_collect_error(lambda: api(status=304).get(URL)).status, 304)

    def test_a_long_message_is_bounded(self):
        error = OSError("x" * 5000 + "\nsecond line")
        raised = self.assert_collect_error(lambda: api(at_read=error).get(URL))
        self.assertTrue(str(raised).endswith("..."))

    def test_the_collector_classifies_each_as_a_collector_failure(self):
        for name, error in {**AT_READ, **AT_OPEN}.items():
            with self.subTest(error=name):
                with self.assertRaises(isolation.CollectorFailure):
                    with isolation.contribution("c", "observation"):
                        api(at_read=error).get(URL)
        for name, body in BODIES.items():
            with self.subTest(body=name):
                with self.assertRaises(isolation.CollectorFailure):
                    with isolation.contribution("c", "observation"):
                        api(body=body).get(URL)

    def test_the_safety_net_classifies_raw_transport_errors(self):
        for name, error in {**AT_READ, **AT_OPEN}.items():
            with self.subTest(error=name):
                with self.assertRaises(isolation.CollectorFailure):
                    with isolation.contribution("c", "observation"):
                        raise error


class TestWellFormedResponsesAreNotConverted(unittest.TestCase):
    """A bug on a response that was read is a processing error."""

    def test_valid_json_is_returned(self):
        self.assertEqual(api(body=b'{"a": 1}').get(URL), {"a": 1})
        self.assertIsNone(api(body=b"").get(URL))
        self.assertEqual(api(body=b"\x00\x01").get_bytes(URL), b"\x00\x01")

    def test_a_missing_key_stays_a_processing_error(self):
        body = json.dumps({"name": "riscv64"}).encode()
        with self.assertRaises(isolation.ProcessingError) as raised:
            with isolation.contribution("c", "observation"):
                collect.branch_head(api(body=body), "riseproject-upstreaming/proj", "riscv64")
        self.assertIsInstance(raised.exception.error, KeyError)

    def test_a_wrong_shape_stays_a_processing_error(self):
        with self.assertRaises(isolation.ProcessingError) as raised:
            with isolation.contribution("c", "observation"):
                api(body=b"[]").paginate(URL, key="jobs")
        self.assertIsInstance(raised.exception.error, TypeError)

    def test_value_and_type_errors_are_never_collector_failures(self):
        for error in (ValueError("bad"), TypeError("bad"), KeyError("bad"),
                      json.JSONDecodeError("bad", "x", 0)):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(isolation.ProcessingError):
                    with isolation.contribution("c", "observation"):
                        raise error


if __name__ == "__main__":
    unittest.main()

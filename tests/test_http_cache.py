# SPDX-FileCopyrightText: 2026 The RISE Project
# SPDX-License-Identifier: MIT
"""The HTTP cache of the reads: always revalidated, never holding a token."""

import base64
import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import collect
import dev_pr
import provisioning
from tests.test_collect_read_errors import Response, Session


def query(path, sql, *args):
    db = sqlite3.connect(path)
    try:
        rows = db.execute(sql, args).fetchall()
        db.commit()
        return rows
    finally:
        db.close()


TOKEN = "ghs_SECRETSECRETSECRET"


class Server:
    """A GitHub that answers ``/doc`` with an ETag and honors ``If-None-Match``."""

    def __init__(self):
        self.body, self.etag, self.requests, self.status = {"v": 1}, '"e1"', [], 200
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/old":
                    self.send_response(301)
                    self.send_header("Location", "/doc")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                validators = self.path.startswith("/doc")
                if validators and self.headers.get("If-None-Match") == outer.etag:
                    self.send_response(304)
                    self.send_header("ETag", outer.etag)
                    self.end_headers()
                    return
                body = json.dumps(outer.body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if validators:
                    self.send_header("ETag", outer.etag)
                # A response with a max-age is still revalidated.
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cache", "http-cache.sqlite")
        # The loopback server is reached directly, not through a proxy.
        env = mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1",
                                           collect.HTTP_CACHE_ENV: self.path})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.forget_sessions)

    def forget_sessions(self):
        for session in collect._CACHED_SESSIONS.values():
            session.close()
        collect._CACHED_SESSIONS.clear()

    def api(self, token=TOKEN, **kwargs):
        return collect.Api(token=token, base=self.server.base, **kwargs)

    def conditional(self):
        return [h.get("If-None-Match") for _p, h in self.server.requests]


class TestRevalidation(CacheCase):
    def test_a_second_read_is_conditional_and_a_304_returns_the_cached_body(self):
        api = self.api()
        self.assertEqual(api.get("/doc"), {"v": 1})
        self.assertEqual(api.get("/doc"), {"v": 1})
        # Even a response that allows caching for an hour is asked about again.
        self.assertEqual(self.conditional(), [None, '"e1"'])
        self.assertTrue(Path(self.path).exists())

    def test_a_new_run_reuses_the_stored_file(self):
        self.assertEqual(self.api().get("/doc"), {"v": 1})
        self.forget_sessions()
        self.assertEqual(self.api().get("/doc"), {"v": 1})
        self.assertEqual(self.conditional(), [None, '"e1"'])

    def test_a_changed_response_replaces_the_cached_one(self):
        api = self.api()
        api.get("/doc")
        self.server.body, self.server.etag = {"v": 2}, '"e2"'
        self.assertEqual(api.get("/doc"), {"v": 2})
        self.assertEqual(api.get("/doc"), {"v": 2})
        self.assertEqual(self.conditional(), [None, '"e1"', '"e2"'])

    def test_a_response_without_a_validator_is_fetched_again(self):
        api = self.api()
        self.assertEqual(api.get("/plain"), {"v": 1})
        self.server.body = {"v": 2}
        self.assertEqual(api.get("/plain"), {"v": 2})
        self.assertEqual(self.conditional(), [None, None])

    def test_pagination_revalidates_every_page(self):
        api = self.api()
        api.get("/doc?page=2")
        api.paginate("/doc?page=2")
        self.assertEqual(self.conditional(), [None, '"e1"'])

    def test_an_error_is_never_answered_from_the_cache(self):
        api = self.api()
        api.get("/doc")
        self.server.status = 502
        with self.assertRaises(collect.CollectError) as raised:
            api.get("/doc")
        self.assertEqual(raised.exception.status, 502)
        self.server.status = 200
        self.assertEqual(api.get("/doc"), {"v": 1})

    def test_the_entries_are_json_not_pickles(self):
        self.api().get("/doc")
        ((value,),) = query(self.path, "select value from responses")
        self.assertIsInstance(value, str)
        self.assertEqual(json.loads(value)["status_code"], 200)


class TestNoSecrets(CacheCase):
    def test_the_token_is_neither_in_the_key_nor_stored(self):
        self.api().get("/doc")
        self.api().get("/doc")
        keys = [k for (k,) in query(self.path, "select key from responses")]
        redirects = query(self.path, "select * from redirects")
        self.assertEqual(len(keys), 1)
        raw = Path(self.path).read_bytes()
        for name in os.listdir(os.path.dirname(self.path)):
            raw += (Path(os.path.dirname(self.path)) / name).read_bytes()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(TOKEN.encode(), repr(redirects).encode())
        self.assertNotIn(b"Bearer", raw)

    def test_the_key_does_not_depend_on_the_token(self):
        self.api(token="one").get("/doc")
        self.api(token="two").get("/doc")
        self.api(token="").get("/doc")
        # The server is still asked, with the token of the request.
        self.assertEqual(self.conditional(), [None, '"e1"', '"e1"'])
        self.assertEqual([h.get("Authorization") for _p, h in self.server.requests],
                         ["Bearer one", "Bearer two", None])
        self.assertEqual(query(self.path, "select count(*) from responses"), [(1,)])


class TestRedirects(CacheCase):
    def test_a_redirected_response_is_never_stored_and_the_token_is_not_in_the_file(self):
        api = self.api()
        self.assertEqual(api.get("/old"), {"v": 1})
        self.assertEqual(api.get("/old"), {"v": 1})
        raw = Path(self.path).read_bytes()
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertNotIn(b"Bearer", raw)
        keys = [k for (k,) in query(self.path, "select key from responses")]
        self.assertEqual(len(keys), 1)  # the target, stored without any redirect hop
        self.assertEqual(query(self.path, "select count(*) from redirects"), [(0,)])
        # The target is still revalidated; the redirect itself is asked every time.
        self.assertEqual([p for p, _h in self.server.requests], ["/old", "/doc", "/old", "/doc"])
        self.assertEqual(self.conditional(), [None, None, None, '"e1"'])


class TestPlantedRows(CacheCase):
    """The file is restored from an earlier run: what it holds is checked on opening."""

    def plant(self, **changes):
        self.api().get("/doc")
        ((key, value),) = query(self.path, "select key, value from responses")
        row = json.loads(value)
        row.update(changes.pop("row", {}))
        for name in changes.pop("drop_headers", ()):
            row["headers"] = {k: v for k, v in row["headers"].items() if k.lower() != name}
        row["_content"] = base64.b64encode(b'{"forged": true}').decode()
        query(self.path, "update responses set value = ? where key = ?", json.dumps(row), key)
        self.forget_sessions()
        self.server.requests.clear()

    def test_a_row_without_a_validator_or_expiry_is_never_served(self):
        for name, changes in {
                "no validator and no expiry": dict(row={"expires": None},
                                                   drop_headers=("etag", "last-modified")),
                "no expiry": dict(row={"expires": None}),
                "no validator": dict(drop_headers=("etag", "last-modified")),
                "an expiry in the future": dict(row={"expires": "2999-01-01T00:00:00+00:00"})}.items():
            with self.subTest(name):
                self.plant(**changes)
                self.assertEqual(self.api().get("/doc"), {"v": 1})
                self.assertEqual(len(self.server.requests), 1)  # the server was asked
                self.assertIsNone(self.server.requests[0][1].get("If-None-Match"))
                self.forget_sessions()
                os.unlink(self.path)

    def test_a_row_that_cannot_be_read_is_dropped(self):
        self.api().get("/doc")
        query(self.path, "update responses set value = ?", "{not json")
        self.forget_sessions()
        self.assertEqual(self.api().get("/doc"), {"v": 1})
        self.assertEqual(self.conditional(), [None, None])

    def test_a_row_of_the_wrong_shape_never_fails_a_read(self):
        shapes = {
            "created_at is a string": lambda d: d.update(created_at="garbage"),
            "headers is a number": lambda d: d.update(headers=5),
            "expires is a number": lambda d: d.update(expires=5),
            "status_code is a string": lambda d: d.update(status_code="x"),
            "request is null": lambda d: d.update(request=None),
            "a JSON list": lambda d: [1],
            "a JSON string": lambda d: "x",
            "an empty object": lambda d: {},
        }
        for name, shape in shapes.items():
            with self.subTest(name):
                self.api().get("/doc")
                ((key, value),) = query(self.path, "select key, value from responses")
                row = json.loads(value)
                row = shape(row) if shape(row) is not None else row
                query(self.path, "update responses set value = ? where key = ?", json.dumps(row), key)
                self.forget_sessions()
                self.server.requests.clear()
                self.assertEqual(self.api().get("/doc"), {"v": 1})
                self.assertFalse(collect._CACHE_DISABLED)
                self.assertIsNone(self.server.requests[0][1].get("If-None-Match"))
                # The file works again afterwards.
                self.assertEqual(self.api().get("/doc"), {"v": 1})
                self.assertEqual(self.conditional(), [None, '"e1"'])
                self.forget_sessions()
                os.unlink(self.path)

    def test_a_good_row_survives_the_opening(self):
        self.api().get("/doc")
        self.forget_sessions()
        self.api()
        self.assertEqual(query(self.path, "select count(*) from responses"), [(1,)])


class TestCacheFailure(CacheCase):
    def test_a_sqlite_error_falls_back_to_an_uncached_read_for_the_process(self):
        api = self.api()
        self.assertEqual(api.get("/doc"), {"v": 1})
        with mock.patch.object(api._session, "get",
                               side_effect=sqlite3.OperationalError("database is locked")):
            self.assertEqual(api.get("/doc"), {"v": 1})
        self.assertTrue(collect._CACHE_DISABLED)
        self.assertEqual(self.conditional(), [None, None])
        # Every later client is uncached, and the PyPI reads too.
        self.assertNotIsInstance(self.api()._session, collect._CachedSession)
        self.assertNotIsInstance(collect._read_session(), collect._CachedSession)

    def test_a_cache_that_cannot_be_opened_is_not_a_failure(self):
        blocker = Path(self.tmp.name, "blocker")
        blocker.write_text("a file, not a directory")
        os.environ[collect.HTTP_CACHE_ENV] = str(blocker / "http-cache.sqlite")
        self.assertEqual(self.api().get("/doc"), {"v": 1})
        self.assertTrue(collect._CACHE_DISABLED)

    def test_pypi_falls_back_too(self):
        session = collect._read_session()
        self.assertIsInstance(session, collect._CachedSession)
        with mock.patch.object(session, "get", side_effect=sqlite3.OperationalError("full")), \
                mock.patch.object(collect._Session, "get",
                                  return_value=Response(body=b'{"info": {}}')):
            self.assertEqual(collect.pypi_project("proj"), {"info": {}})
        self.assertTrue(collect._CACHE_DISABLED)

    def setUp(self):
        super().setUp()
        collect._CACHE_DISABLED = False
        self.addCleanup(setattr, collect, "_CACHE_DISABLED", False)


class TestWhatIsNotCached(CacheCase):
    def test_downloads_are_not_cached(self):
        api = self.api()
        self.assertEqual(api.get_bytes("/doc"), b'{"v": 1}')
        self.assertEqual(api.get_bytes("/doc"), b'{"v": 1}')
        self.assertEqual(self.conditional(), [None, None])
        self.assertFalse(Path(self.path).exists() and self._rows())

    def _rows(self):
        return query(self.path, "select count(*) from responses")[0][0]

    def test_a_download_does_not_reuse_a_cached_read(self):
        api = self.api()
        api.get("/doc")
        api.get_bytes("/doc")
        self.assertEqual(self.conditional(), [None, None])

    def test_the_clients_that_write_never_read_through_the_cache(self):
        for client in (provisioning.WriteApi("t"), dev_pr.Api(token="t")):
            client.base = self.server.base
            client.get("/doc")
            client.get("/doc")
        self.assertEqual(self.conditional(), [None] * 4)
        self.assertFalse(Path(self.path).exists())

    def test_a_write_client_follows_no_redirect(self):
        client = provisioning.WriteApi("t")
        client.base = self.server.base
        with self.assertRaisesRegex(provisioning.ProvisionError, "redirect refused"):
            client.get("/old")
        self.assertEqual([p for p, _h in self.server.requests], ["/old"])
        # The clients that only read through a write token still follow, as before.
        other = dev_pr.Api(token="t")
        other.base = self.server.base
        self.assertEqual(other.get("/old"), {"v": 1})

    def test_only_a_get_is_cacheable(self):
        session = collect._cached_session(self.path)
        self.addCleanup(session.close)
        self.assertEqual(tuple(session.settings.allowable_methods), ("GET",))
        self.assertEqual(tuple(session.settings.allowable_codes), (200,))
        self.assertTrue(session.settings.always_revalidate)
        self.assertEqual(session.settings.expire_after, 0)
        self.assertFalse(session.settings.cache_control)
        self.assertFalse(session.settings.stale_if_error)

    def test_without_the_variable_no_cache_file_is_created(self):
        del os.environ[collect.HTTP_CACHE_ENV]
        api = self.api()
        api.get("/doc")
        api.get("/doc")
        self.assertEqual(self.conditional(), [None, None])
        self.assertFalse(Path(self.tmp.name, "cache").exists())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_an_injected_session_is_used_as_it_is(self):
        session = Session(lambda url, **kw: Response(body=b"[1]"))
        self.assertEqual(collect.Api(token="", session=session).get("/x"), [1])
        self.assertFalse(Path(self.path).exists())


class TestBounds(CacheCase):
    def test_old_entries_are_dropped_when_the_cache_is_opened(self):
        self.api().get("/doc")
        self.api().get("/plain")
        ((value,),) = query(self.path, "select value from responses limit 1")
        old = json.loads(value)
        old["created_at"] = "2020-01-01T00:00:00+00:00"
        query(self.path, "update responses set value = ?", json.dumps(old))
        self.forget_sessions()
        self.api()
        self.assertEqual(self.conditional()[0], None)
        self.assertEqual(query(self.path, "select count(*) from responses"), [(0,)])

    def test_a_file_over_the_size_bound_is_emptied(self):
        self.api().get("/doc")
        with mock.patch.object(collect, "HTTP_CACHE_MAX_BYTES", 10):
            self.forget_sessions()
            self.api()
        self.assertEqual(query(self.path, "select count(*) from responses"), [(0,)])

    def test_a_damaged_file_does_not_fail_the_read(self):
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_bytes(b"this is not a database" * 100)
        self.assertEqual(self.api().get("/doc"), {"v": 1})

    def test_the_age_bound(self):
        self.assertEqual(collect.HTTP_CACHE_MAX_AGE, timedelta(days=7))


class TestPyPI(CacheCase):
    def test_a_missing_project_is_none_and_other_errors_are_collector_failures(self):
        def reader(status, body=b"{}", error=None):
            return Session(lambda url, **kw: Response(body=body, status=status, error=error))
        self.assertIsNone(collect.pypi_project("proj", session=reader(404)))
        self.assertEqual(collect.pypi_project("proj", session=reader(200, b'{"info": {}}')),
                         {"info": {}})
        with self.assertRaisesRegex(collect.CollectError, "PyPI proj: HTTP 503"):
            collect.pypi_project("proj", session=reader(503))
        with self.assertRaisesRegex(collect.CollectError, "PyPI proj"):
            collect.pypi_project("proj", session=reader(200, b"<html>"))
        with self.assertRaisesRegex(collect.CollectError, "PyPI proj"):
            collect.pypi_project("proj", session=reader(200, error=TimeoutError("slow")))
        with self.assertRaises(collect.CollectError):
            collect.pypi_release("../etc", session=reader(200))

    def test_pypi_reads_go_through_the_same_revalidated_cache(self):
        session = collect._read_session()
        with mock.patch.object(session, "get", wraps=session.get) as get:
            with mock.patch.object(collect, "_read_session", return_value=session):
                try:
                    collect.pypi_project("proj")
                except collect.CollectError:
                    pass
        self.assertEqual(get.call_args.args[0], "https://pypi.org/pypi/proj/json")
        self.assertIsInstance(session, collect._CachedSession)


if __name__ == "__main__":
    unittest.main()

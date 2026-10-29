"""The worker's standard-library S3 client: signatures and request handling, offline.

The expected signatures were produced by botocore's S3SigV4Auth for the same requests."""

import hashlib
import io
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from helpers import PACKAGE

sys.path.insert(0, str(PACKAGE))
import s3lite  # noqa: E402

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
CREDENTIALS = {"key": "AKIDEXAMPLE", "secret": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "token": "tok/en+="}
HOST = "bkt.s3.us-east-1.amazonaws.com"


def signature(method, key, query, body=b""):
    extra = {"content-length": str(len(body))} if method == "PUT" else {}
    headers = s3lite.sign(method, HOST, "/" + s3lite.quote(key, safe="-_.~/"), query, extra,
                          hashlib.sha256(body).hexdigest(), CREDENTIALS, "us-east-1", NOW)
    return headers["authorization"].rsplit("=", 1)[1]


class SignatureTests(unittest.TestCase):
    def test_signatures_match_botocore(self):
        self.assertEqual(signature("GET", "runs/2026/0/1/out/a b+c.json", []),
                         "7eaa7e28719f4f87fc95a0e3c869736ec8b34a661726c390fb45ba4b5433a832")
        self.assertEqual(signature("PUT", "inputs/abc", [], b"hello"),
                         "71f251959af3de521ac43bc391393bcd1fe6a68c997e94cf4ee7feaed9af1e11")
        self.assertEqual(signature("GET", "", [("list-type", "2"), ("prefix", "runs/x y/"),
                                               ("continuation-token", "a/b+c=")]),
                         "db34f8f2f04846af67ce6e083bc9227937645e1fb0abd8af668f433e6408a417")


class Response(io.BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status


class Server:
    """Answers requests from a script of (status, body) and records what was sent."""

    def __init__(self, *answers):
        self.answers, self.sent = list(answers), []

    def connect(self, host):
        server = self

        class Connection:
            def request(self, method, target, body=None, headers=None):
                data = body.read() if hasattr(body, "read") else body
                server.sent.append((method, target, data, headers))

            def getresponse(self):
                return Response(*server.answers.pop(0))
        return Connection()


class Fixed:
    current = None

    def get(self):
        return CREDENTIALS


def store(server):
    return s3lite.S3Store("bkt", "us-east-1", credentials=Fixed(), connect=server.connect, clock=lambda: NOW)


class RequestTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(s3lite.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_put_file_streams_the_body_with_its_hash(self):
        server = Server((200,))
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "f"
            path.write_bytes(b"payload")
            store(server).put_file(path, "runs/x/a b")
        method, target, data, headers = server.sent[0]
        self.assertEqual((method, target, data), ("PUT", "/runs/x/a%20b", b"payload"))
        self.assertEqual(headers["x-amz-content-sha256"], hashlib.sha256(b"payload").hexdigest())
        self.assertEqual(headers["content-length"], "7")

    def test_missing_object_is_none_and_other_client_errors_raise(self):
        missing = b"<Error><Code>NoSuchKey</Code><Message>gone</Message></Error>"
        self.assertIsNone(store(Server((404, missing))).get_bytes("k"))
        with self.assertRaisesRegex(s3lite.S3Error, "AccessDenied"):
            store(Server((403, b"<Error><Code>AccessDenied</Code></Error>"))).get_bytes("k")

    def test_throttling_and_server_errors_are_retried(self):
        server = Server((503, b"<Error><Code>SlowDown</Code></Error>"), (500, b""), (200, b"ok"))
        self.assertEqual(store(server).get_bytes("k"), b"ok")
        self.assertEqual(len(server.sent), 3)

    def test_listing_follows_continuation_tokens(self):
        ns = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
        page1 = ('<ListBucketResult %s><Contents><Key>p/a</Key><Size>1</Size></Contents><IsTruncated>true'
                 '</IsTruncated><NextContinuationToken>t+1</NextContinuationToken></ListBucketResult>' % ns)
        page2 = ('<ListBucketResult %s><Contents><Key>p/b</Key><Size>2</Size></Contents>'
                 '<IsTruncated>false</IsTruncated></ListBucketResult>' % ns)
        server = Server((200, page1.encode()), (200, page2.encode()))
        self.assertEqual(store(server).list("p/"), [("p/a", 1), ("p/b", 2)])
        self.assertIn("continuation-token=t%2B1", server.sent[1][1])


if __name__ == "__main__":
    unittest.main()

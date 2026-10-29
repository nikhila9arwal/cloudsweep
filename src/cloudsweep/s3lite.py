"""A small S3 client using only the Python standard library (3.8+), for the worker.

The worker runs inside the project's own toolchain image, which needs only python3 and
git: no AWS SDK. This client signs requests with Signature Version 4 and supports what the
worker needs: GetObject, PutObject (single request, at most 5 GiB) and ListObjectsV2.
Credentials come from the ECS task-role endpoint that AWS Batch provides, or from the
standard AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN variables.
"""

import hashlib
import hmac
import http.client
import json
import os
import shutil
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone

MAX_PUT_BYTES = 5 * 1024 ** 3
NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
ECS_CREDENTIALS = "169.254.170.2"
RETRIES = 8


class S3Error(Exception):
    def __init__(self, status, code, message):
        super().__init__("S3 %s %s: %s" % (status, code, message))
        self.status, self.code = status, code


def quote(text, safe="-_.~"):
    return urllib.parse.quote(text, safe=safe)


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def signing_key(secret, date, region, service):
    key = _hmac(("AWS4" + secret).encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sign(method, host, path, query, headers, payload_hash, credentials, region, now, service="s3"):
    """Return the headers for a SigV4-signed request. path is already URI-encoded; query is a
    list of (name, value) pairs; headers holds any extra headers to sign."""
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    signed = dict((name.lower(), str(value).strip()) for name, value in headers.items())
    signed.update({"host": host, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash})
    if credentials.get("token"):
        signed["x-amz-security-token"] = credentials["token"]
    names = sorted(signed)
    canonical_query = "&".join("%s=%s" % (quote(k), quote(v)) for k, v in sorted(query))
    canonical = "\n".join([method, path, canonical_query,
                           "".join("%s:%s\n" % (name, signed[name]) for name in names),
                           ";".join(names), payload_hash])
    scope = "%s/%s/%s/aws4_request" % (date, region, service)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signature = hmac.new(signing_key(credentials["secret"], date, region, service), to_sign.encode(),
                         hashlib.sha256).hexdigest()
    signed["authorization"] = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
        credentials["key"], scope, ";".join(names), signature)
    return signed


class Credentials:
    """Task-role credentials from the ECS endpoint, refreshed before they expire."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        self.current, self.expires = None, None

    def get(self):
        now = datetime.now(timezone.utc)
        if self.current and (self.expires is None or self.expires - now > timedelta(minutes=10)):
            return self.current
        env = self.environ
        if env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
            data = self._fetch(env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"])
            self.current = {"key": data["AccessKeyId"], "secret": data["SecretAccessKey"],
                            "token": data.get("Token")}
            expiry = data.get("Expiration")
            self.expires = (datetime.strptime(expiry[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                            if expiry else None)
        elif env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
            self.current = {"key": env["AWS_ACCESS_KEY_ID"], "secret": env["AWS_SECRET_ACCESS_KEY"],
                            "token": env.get("AWS_SESSION_TOKEN")}
            self.expires = None
        else:
            raise S3Error(0, "NoCredentials", "no task-role endpoint or AWS_ACCESS_KEY_ID in the environment")
        return self.current

    def _fetch(self, relative):
        for attempt in range(RETRIES):
            try:
                connection = http.client.HTTPConnection(ECS_CREDENTIALS, timeout=5)
                connection.request("GET", relative)
                response = connection.getresponse()
                body = response.read()
                if response.status == 200:
                    return json.loads(body)
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(min(2 ** attempt, 20))
        raise S3Error(0, "NoCredentials", "the task-role credential endpoint did not answer")


class S3Store:
    """The bucket, with the same interface as core.LocalStore."""

    def __init__(self, bucket, region, credentials=None, connect=None, clock=None):
        self.bucket, self.region = bucket, region
        self.host = "%s.s3.%s.amazonaws.com" % (bucket, region)
        self.credentials = credentials or Credentials()
        self.connect = connect or (lambda host: http.client.HTTPSConnection(host, timeout=120))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _request(self, method, key="", query=(), body=None, payload_hash=None, sink=None, length=None):
        """One request with retries on throttling, server errors and dropped connections.
        body is bytes or a callable returning a fresh file object; sink receives the body."""
        path = "/" + quote(key, safe="-_.~/")
        target = path + ("?" + "&".join("%s=%s" % (quote(k), quote(v)) for k, v in query) if query else "")
        payload_hash = payload_hash or hashlib.sha256(body if isinstance(body, bytes) else b"").hexdigest()
        for attempt in range(RETRIES):
            extra = {}
            if body is not None:
                extra["content-length"] = str(len(body) if isinstance(body, bytes) else length)
            headers = sign(method, self.host, path, list(query), extra, payload_hash,
                           self.credentials.get(), self.region, self.clock())
            headers.pop("host")
            stream = body() if callable(body) else body
            try:
                connection = self.connect(self.host)
                connection.request(method, target, body=stream, headers=headers)
                response = connection.getresponse()
                if response.status < 300:
                    if sink is not None:
                        shutil.copyfileobj(response, sink, 1 << 20)
                        return None
                    return response.read()
                data = response.read()
                code, message = _error(data)
                if response.status < 500 and code not in ("SlowDown", "RequestTimeout", "ExpiredToken",
                                                         "RequestTimeTooSkewed", "InternalError"):
                    raise S3Error(response.status, code, message)
                last = S3Error(response.status, code, message)
                if code == "ExpiredToken":
                    self.credentials.current = None
            except (OSError, http.client.HTTPException) as error:
                last = S3Error(0, type(error).__name__, str(error))
            finally:
                if callable(body) and stream is not None:
                    stream.close()
            if sink is not None:
                sink.seek(0)
                sink.truncate()
            time.sleep(min(0.5 * 2 ** attempt, 30))
        raise last

    def put_bytes(self, data, key):
        self._request("PUT", key, body=data)

    def put_file(self, local, key):
        size = os.path.getsize(local)
        if size > MAX_PUT_BYTES:
            raise S3Error(0, "EntityTooLarge", "%s is %d bytes; single uploads hold at most 5 GiB" % (local, size))
        digest = hashlib.sha256()
        with open(local, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        self._request("PUT", key, body=lambda: open(local, "rb"), payload_hash=digest.hexdigest(), length=size)

    def get_bytes(self, key):
        try:
            return self._request("GET", key, payload_hash=hashlib.sha256(b"").hexdigest())
        except S3Error as error:
            if error.status == 404:
                return None
            raise

    def get_file(self, key, local):
        with open(local, "wb") as sink:
            self._request("GET", key, sink=sink)

    def list(self, prefix):
        found, token = [], None
        while True:
            query = [("list-type", "2"), ("prefix", prefix)] + ([("continuation-token", token)] if token else [])
            root = ElementTree.fromstring(self._request("GET", "", query=query))
            for item in root.findall(NAMESPACE + "Contents"):
                found.append((item.find(NAMESPACE + "Key").text, int(item.find(NAMESPACE + "Size").text)))
            if (root.findtext(NAMESPACE + "IsTruncated") or "false") != "true":
                return found
            token = root.findtext(NAMESPACE + "NextContinuationToken")


def _error(data):
    try:
        root = ElementTree.fromstring(data)
        return root.findtext("Code") or "Unknown", root.findtext("Message") or ""
    except ElementTree.ParseError:
        return "Unknown", data[:200].decode("utf-8", "replace")

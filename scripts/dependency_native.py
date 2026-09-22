"""가격 공급자를 호출하지 않는 실제 wheel의 메모리 검사."""
from __future__ import annotations

import importlib.metadata
import re
import ssl
from datetime import datetime
from zoneinfo import ZoneInfo
import zoneinfo


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def guard_transports(reject):
    import requests
    import curl_cffi
    # C libcurl 전송은 Python socket audit를 우회하므로 public 전송 진입점을 막는다.
    curl_cffi.Curl.perform = lambda *a, **k: reject("NATIVE_NETWORK")
    curl_cffi.AsyncCurl.add_handle = lambda *a, **k: reject("NATIVE_NETWORK")
    requests.sessions.Session.request = lambda *a, **k: reject("NETWORK")
    return requests, curl_cffi


def verify(source, site, reject):
    expected = dict(re.findall(r"^([\w.-]+)==([^\s\\]+)",
                              (source / "locks/dependency-smoke.txt").read_text(), re.MULTILINE))
    expected = {normalize(k): v for k, v in expected.items()}
    installed = {normalize(d.metadata["Name"]): d.version
                 for d in importlib.metadata.distributions(path=[str(site)])}
    assert expected and all(installed.get(k) == v for k, v in expected.items()), "PACKAGE_MISMATCH"
    assert not set(installed) - set(expected) - {"pip", "setuptools", "wheel"}, "UNLOCKED_PACKAGE"

    requests, curl_cffi = guard_transports(reject)
    import certifi
    import numpy as np
    import pandas as pd
    import cffi
    import yfinance
    # Ticker 생성·history·download는 검사 목적이 아니다.
    yfinance.Ticker = lambda *a, **k: reject("PROVIDER")
    yfinance.download = lambda *a, **k: reject("PROVIDER")

    prepared = requests.Request("GET", "https://example.invalid/synthetic", params={"n": "1"}).prepare()
    assert prepared.url == "https://example.invalid/synthetic?n=1"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    assert context.cert_store_stats()["x509_ca"] > 0
    curl = curl_cffi.Curl()
    curl.close()
    ffi = cffi.FFI()
    assert ffi.new("int *", 7)[0] == 7

    values = np.array([100.0, 101.25, 102.5])
    frame = pd.DataFrame({"Close": values}, index=pd.date_range("2001-01-01", periods=3, tz="UTC"))
    assert float(frame["Close"].iloc[-1]) == 102.5 and np.isfinite(values).all()
    assert not np.isfinite(np.array([np.nan, np.inf])).any()
    offsets = {"UTC": (0, 0), "Asia/Seoul": (32400, 32400), "America/New_York": (-18000, -14400)}
    assert zoneinfo.TZPATH == (), "SYSTEM_TZPATH_ACTIVE"
    for zone, expected_offsets in offsets.items():
        for month, seconds in zip((1, 7), expected_offsets):
            value = datetime(2001, month, 15, 12)
            assert value.replace(tzinfo=ZoneInfo(zone)).utcoffset().total_seconds() == seconds
            assert pd.Timestamp(value).tz_localize(zone).utcoffset().total_seconds() == seconds

    from lxml import etree
    from bs4 import BeautifulSoup
    assert etree.fromstring(b"<root><price>7</price></root>").findtext("price") == "7"
    assert BeautifulSoup("<p>synthetic</p>", "lxml").p.text == "synthetic"
    from google.protobuf.struct_pb2 import Struct
    message = Struct(); message.update({"price": 7.0})
    restored = Struct(); restored.ParseFromString(message.SerializeToString())
    assert restored["price"] == 7.0
    from peewee import SqliteDatabase
    db = SqliteDatabase(":memory:")
    try:
        assert db.execute_sql("select 7").fetchone()[0] == 7
    finally:
        db.close()
    # guard 자체의 차단 시험은 별도 프로세스에서 수행한다. 여기서는 0 이벤트가 필수다.
    return {"packages": len(expected), "versions": expected, "zone_checks": 6, "system_tzpath_empty": True,
            "checks": ["requests_prepare", "certifi_ssl", "curl_native", "cffi_memory",
                       "numpy_pandas", "zoneinfo_pandas", "lxml_bs4", "protobuf", "sqlite", "yfinance_import"]}

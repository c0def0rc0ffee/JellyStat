#!/usr/bin/env python3
"""
<summary>
Tests that every JellyStat call to Jellyfin authenticates with the Authorization
header, because Jellyfin 12 answers 401 to the old X-Emby-Token header.
</summary>
<remarks>
No Kodi and no server: kodi_stubs stands in for the Kodi modules and urlopen is
replaced with a fake that records the request. Exits non zero when a check fails.

Usage:
    python3 tests/test_auth.py
</remarks>
"""

import io
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kodi_stubs  # noqa: E402

kodi_stubs.install()

import main  # noqa: E402
import media  # noqa: E402
import ratings  # noqa: E402

FAILURES = []
SENT = []
EXPECTED = 'MediaBrowser Token="tok"'


class FakeResponse(io.BytesIO):
    """
    <summary>
    A urlopen result: a context managed byte stream with a status and headers.
    </summary>
    """
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        """
        <summary>
        Enter the with block.
        </summary>
        <returns>This response.</returns>
        """
        return self

    def __exit__(self, *args):
        """
        <summary>
        Leave the with block.
        </summary>
        <param name="args">The exception details, ignored.</param>
        <returns>False, so an exception carries on.</returns>
        """
        return False


def fake_urlopen(request, timeout=None, context=None):
    """
    <summary>
    Stand in for urllib.request.urlopen, recording the request.
    </summary>
    <param name="request">The Request being sent.</param>
    <param name="timeout">Ignored.</param>
    <param name="context">Ignored.</param>
    <returns>A response holding an empty JSON object.</returns>
    """
    SENT.append(request)
    return FakeResponse(b"{}")


def check(condition, label):
    """
    <summary>
    Record a failed check.
    </summary>
    <param name="condition">True when the check passed.</param>
    <param name="label">What was being checked.</param>
    """
    if not condition:
        FAILURES.append(label)
        print("FAIL: %s" % label)


def check_last(label):
    """
    <summary>
    Check the newest recorded request sent the right header and not the old one.
    </summary>
    <param name="label">Which call sent it.</param>
    """
    request = SENT[-1]
    check(request.get_header("Authorization") == EXPECTED, label + ": Authorization")
    check(request.get_header("X-emby-token") is None, label + ": no X-Emby-Token")


def main_test():
    """
    <summary>
    Run the three call sites through the fake and report.
    </summary>
    <returns>The exit code for the process.</returns>
    """
    urllib.request.urlopen = fake_urlopen

    main.api_get("http://server", "tok", "/System/Info")
    check_last("main.api_get")

    ratings._request("http://server/Items/a", "tok", "GET")
    check_last("ratings._request")

    media.core.get_credentials = lambda: {"base": "http://server", "token": "tok",
                                          "user_id": "u"}
    media.fetch_image("0123456789abcdef0123456789abcdef", "Primary", 400)
    check_last("media image fetch")

    print("test_auth: %d failed" % len(FAILURES))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main_test())

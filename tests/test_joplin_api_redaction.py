"""The Joplin token must never survive into an exception a caller can see.

The REST API authenticates by query parameter, so the token is in every
request URL, and `requests` embeds that URL in the string form of its
exceptions. These tests pin the two failure branches of `_make_request` that
would otherwise hand the token to the container log and the MCP client.
"""

import unittest
from unittest import mock

import requests

from src.joplin.joplin_api import JoplinAPI, JoplinAPIError

TOKEN = "s3cr3t_token_that_is_definitely_over_32_chars"
# The shape requests/urllib3 produce on a connect timeout: the full URL, token
# query parameter included, right there in the exception message.
LEAKY_URL = f"http://joplin:41184/notes?fields=id&token={TOKEN}"


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.api = JoplinAPI(token=TOKEN, base_url="http://joplin:41184")

    def _raise(self, exc):
        return mock.patch("requests.request", side_effect=exc)

    def test_connect_timeout_does_not_leak_token(self):
        # ConnectTimeout subclasses Timeout, so it hits the Timeout branch first.
        exc = requests.exceptions.ConnectTimeout(
            f"HTTPConnectionPool(host='joplin', port=41184): Max retries "
            f"exceeded with url: {LEAKY_URL}"
        )
        with self._raise(exc):
            with self.assertRaises(JoplinAPIError) as caught:
                self.api._make_request("GET", "notes")

        message = str(caught.exception)
        self.assertNotIn(TOKEN, message)
        self.assertIn("***REDACTED***", message)

    def test_generic_request_exception_does_not_leak_token(self):
        exc = requests.exceptions.ConnectionError(
            f"Failed to establish a new connection to {LEAKY_URL}"
        )
        with self._raise(exc):
            with self.assertRaises(JoplinAPIError) as caught:
                self.api._make_request("GET", "notes")

        self.assertNotIn(TOKEN, str(caught.exception))

    def test_read_timeout_raises_joplin_api_error(self):
        # A plain read timeout carries no URL, but must still surface as the
        # sanitized error type rather than a raw requests exception.
        with self._raise(requests.exceptions.ReadTimeout("read timed out")):
            with self.assertRaises(JoplinAPIError):
                self.api._make_request("GET", "notes")


if __name__ == "__main__":
    unittest.main()

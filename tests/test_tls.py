from __future__ import annotations

import os
import ssl
import unittest
from unittest.mock import patch

import certifi

from nwn2_workshop_manager.updater import request, tls_context


class TrustTests(unittest.TestCase):
    def test_missing_host_ca_uses_bundled_roots_with_verification(self):
        with patch.dict(os.environ, {}, clear=True), patch("nwn2_workshop_manager.updater.SYSTEM_CA_FILES", ()):
            context = tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), 0)

    def test_host_bundle_selected(self):
        with patch.dict(os.environ, {}, clear=True), patch("nwn2_workshop_manager.updater.SYSTEM_CA_FILES", (certifi.where(),)), patch(
                "nwn2_workshop_manager.updater.certifi.where", side_effect=AssertionError("fallback not needed")):
            context = tls_context()
        self.assertTrue(context.check_hostname)

    def test_invalid_optional_system_bundle_falls_back(self):
        with patch.dict(os.environ, {}, clear=True), patch("nwn2_workshop_manager.updater.SYSTEM_CA_FILES", (__file__,)):
            self.assertGreater(len(tls_context().get_ca_certs()), 0)

    def test_invalid_explicit_override_fails_closed(self):
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/missing/nwn2-ca.pem"}, clear=True), self.assertRaises(OSError):
            tls_context()

    def test_every_request_uses_verified_context(self):
        with patch("nwn2_workshop_manager.updater.urllib.request.urlopen") as opening:
            request("https://api.github.com/")
        context = opening.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

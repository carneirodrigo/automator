from __future__ import annotations

import unittest
from unittest.mock import patch
from engine.work.credential_tester import (
    CredentialTester,
    CredentialType,
)


class CredentialTesterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tester = CredentialTester(timeout=5)

    def test_api_key_detects_empty_key(self) -> None:
        result = self.tester.test_api_key("", "openai")
        self.assertFalse(result.valid)
        self.assertEqual(result.credential_type, CredentialType.API_KEY)
        self.assertIn("empty", result.message.lower())

    def test_api_key_detects_unknown_service(self) -> None:
        result = self.tester.test_api_key("some-key", "unknown-service")
        self.assertFalse(result.valid)
        self.assertIn("no test endpoint", result.message.lower())

    def test_api_key_accepts_custom_endpoint(self) -> None:
        # With custom endpoint but bad network, should get connection error (not empty key error)
        result = self.tester.test_api_key(
            "test-key", "custom", endpoint="http://localhost:99999"
        )
        # Should fail but not for empty key reason
        self.assertFalse(result.valid)
        self.assertNotIn("empty", result.message.lower())

    @patch.object(CredentialTester, "_test_http_endpoint")
    def test_api_key_uses_anthropic_headers(self, mock_http) -> None:
        mock_http.return_value = self.tester.test_aws_credentials(
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )

        self.tester.test_api_key("anthropic-secret", "anthropic")

        _, kwargs = mock_http.call_args
        self.assertEqual(kwargs["headers"]["x-api-key"], "anthropic-secret")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", kwargs["headers"])

    def test_basic_auth_detects_missing_credentials(self) -> None:
        result = self.tester.test_basic_auth("", "", "http://example.com")
        self.assertFalse(result.valid)
        self.assertEqual(result.credential_type, CredentialType.BASIC_AUTH)

    def test_basic_auth_detects_missing_endpoint(self) -> None:
        result = self.tester.test_basic_auth("user", "pass", "")
        self.assertFalse(result.valid)
        self.assertIn("endpoint", result.message.lower())

    def test_bearer_token_detects_empty_token(self) -> None:
        result = self.tester.test_bearer_token("", "http://example.com")
        self.assertFalse(result.valid)
        self.assertEqual(result.credential_type, CredentialType.BEARER_TOKEN)
        self.assertIn("empty", result.message.lower())

    def test_bearer_token_detects_missing_endpoint(self) -> None:
        result = self.tester.test_bearer_token("token123", "")
        self.assertFalse(result.valid)
        self.assertIn("endpoint", result.message.lower())

    def test_aws_credentials_detects_missing_fields(self) -> None:
        result = self.tester.test_aws_credentials("", "")
        self.assertFalse(result.valid)
        self.assertEqual(result.credential_type, CredentialType.AWS)

    def test_aws_credentials_validates_access_key_format(self) -> None:
        # Invalid format (doesn't start with AKIA)
        result = self.tester.test_aws_credentials("INVALID123", "secret")
        self.assertFalse(result.valid)
        self.assertIn("invalid format", result.message.lower())

    def test_aws_credentials_validates_secret_length(self) -> None:
        # Valid access key format but secret too short
        result = self.tester.test_aws_credentials("AKIAIOSFODNN7EXAMPLE", "short")
        self.assertFalse(result.valid)
        self.assertIn("too short", result.message.lower())

    def test_aws_credentials_accepts_valid_format(self) -> None:
        # Valid format (will pass structure check)
        result = self.tester.test_aws_credentials(
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.credential_type, CredentialType.AWS)
        self.assertIn("structurally valid", result.message.lower())

    def test_azure_credentials_detects_missing_fields(self) -> None:
        result = self.tester.test_azure_credentials("", "", "")
        self.assertFalse(result.valid)
        self.assertEqual(result.credential_type, CredentialType.AZURE)
        self.assertIn("missing", result.message.lower())

    def test_azure_credentials_validates_tenant_id_format(self) -> None:
        # Invalid UUID format for tenant
        result = self.tester.test_azure_credentials(
            "not-a-uuid",
            "12345678-1234-1234-1234-123456789012",
            "secret123"
        )
        self.assertFalse(result.valid)
        self.assertIn("tenant", result.message.lower())

    def test_azure_credentials_validates_client_id_format(self) -> None:
        # Invalid UUID format for client
        result = self.tester.test_azure_credentials(
            "12345678-1234-1234-1234-123456789012",
            "not-a-uuid",
            "secret123"
        )
        self.assertFalse(result.valid)
        self.assertIn("client", result.message.lower())

    def test_azure_credentials_validates_secret_length(self) -> None:
        # Valid UUIDs but secret too short
        result = self.tester.test_azure_credentials(
            "12345678-1234-1234-1234-123456789012",
            "12345678-1234-1234-1234-123456789012",
            "short"
        )
        self.assertFalse(result.valid)
        self.assertIn("too short", result.message.lower())

    def test_azure_credentials_accepts_valid_format(self) -> None:
        # All valid formats
        result = self.tester.test_azure_credentials(
            "12345678-1234-1234-1234-123456789012",
            "87654321-4321-4321-4321-210987654321",
            "a_valid_long_secret_string_12345"
        )
        self.assertTrue(result.valid)
        self.assertEqual(result.credential_type, CredentialType.AZURE)
        self.assertIn("structurally valid", result.message.lower())

    def test_result_converts_to_dict(self) -> None:
        result = self.tester.test_api_key("", "openai")
        data = result.to_dict()
        self.assertIsInstance(data, dict)
        self.assertIn("valid", data)
        self.assertIn("credential_type", data)
        self.assertIn("message", data)
        self.assertEqual(data["credential_type"], "api_key")


if __name__ == "__main__":
    unittest.main()

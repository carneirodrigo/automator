"""
Local authentication testing layer for validating credentials before agent execution.

This module tests if provided credentials actually work by attempting basic
authentication operations. Used by agents (typically coding) to validate that
credentials are usable before proceeding with credentialed work.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

try:
    import urllib.request
    import urllib.error
except ImportError:
    urllib = None  # type: ignore


class CredentialType(Enum):
    """Supported credential authentication types."""
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    BASIC_AUTH = "basic_auth"
    AWS = "aws"
    AZURE = "azure"
    OAUTH2 = "oauth2"
    CUSTOM = "custom"


@dataclass
class CredentialTestResult:
    """Result of a credential validation attempt."""
    valid: bool
    credential_type: CredentialType
    message: str
    error_detail: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "valid": self.valid,
            "credential_type": self.credential_type.value,
            "message": self.message,
            "error_detail": self.error_detail,
            "metadata": self.metadata or {},
        }


class CredentialTester:
    """Tests if credentials are valid and usable."""

    def __init__(self, timeout: int = 10):
        """
        Initialize the credential tester.

        Args:
            timeout: HTTP request timeout in seconds
        """
        self.timeout = timeout

    def test_api_key(
        self,
        api_key: str,
        service: str,
        endpoint: str | None = None,
    ) -> CredentialTestResult:
        """
        Test if an API key is valid.

        Args:
            api_key: The API key to test
            service: Service name (e.g., 'openai', 'anthropic', 'github')
            endpoint: Optional custom endpoint to test against

        Returns:
            CredentialTestResult indicating validity
        """
        if not api_key or not api_key.strip():
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.API_KEY,
                message="API key is empty or missing",
                error_detail="Empty string provided",
            )

        service_name = service.lower()

        # Map known services to their test endpoints and auth headers.
        endpoints = {
            "openai": "https://api.openai.com/v1/models",
            "anthropic": "https://api.anthropic.com/v1/models",
            "github": "https://api.github.com/user",
            "stripe": "https://api.stripe.com/v1/account",
        }
        headers_by_service = {
            "openai": {"Authorization": f"Bearer {api_key}"},
            "anthropic": {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            "github": {"Authorization": f"Bearer {api_key}"},
            "stripe": {"Authorization": f"Bearer {api_key}"},
        }

        test_endpoint = endpoint or endpoints.get(service_name)
        if not test_endpoint:
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.API_KEY,
                message=f"No test endpoint known for service: {service}",
                error_detail="Provide a custom endpoint or use a known service",
            )

        return self._test_http_endpoint(
            test_endpoint,
            headers=headers_by_service.get(service_name, {"Authorization": f"Bearer {api_key}"}),
            credential_type=CredentialType.API_KEY,
        )

    def test_bearer_token(
        self,
        token: str,
        endpoint: str,
    ) -> CredentialTestResult:
        """
        Test if a bearer token is valid.

        Args:
            token: The bearer token to test
            endpoint: HTTP endpoint to test against

        Returns:
            CredentialTestResult indicating validity
        """
        if not token or not token.strip():
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.BEARER_TOKEN,
                message="Bearer token is empty or missing",
                error_detail="Empty string provided",
            )

        if not endpoint or not endpoint.strip():
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.BEARER_TOKEN,
                message="No endpoint provided for token validation",
                error_detail="Bearer token validation requires a test endpoint",
            )

        return self._test_http_endpoint(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            credential_type=CredentialType.BEARER_TOKEN,
        )

    def test_basic_auth(
        self,
        username: str,
        password: str,
        endpoint: str,
    ) -> CredentialTestResult:
        """
        Test if basic auth credentials are valid.

        Args:
            username: Username
            password: Password
            endpoint: HTTP endpoint to test against

        Returns:
            CredentialTestResult indicating validity
        """
        if not username or not password:
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.BASIC_AUTH,
                message="Username or password is empty",
                error_detail="Both username and password are required",
            )

        if not endpoint or not endpoint.strip():
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.BASIC_AUTH,
                message="No endpoint provided for auth validation",
                error_detail="Basic auth validation requires a test endpoint",
            )

        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        return self._test_http_endpoint(
            endpoint,
            headers={"Authorization": f"Basic {credentials}"},
            credential_type=CredentialType.BASIC_AUTH,
        )

    def test_aws_credentials(
        self,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
    ) -> CredentialTestResult:
        """
        Test if AWS credentials are valid.

        Args:
            access_key_id: AWS access key ID
            secret_access_key: AWS secret access key
            region: AWS region to test

        Returns:
            CredentialTestResult indicating validity
        """
        if not access_key_id or not secret_access_key:
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AWS,
                message="AWS access key ID or secret access key is missing",
                error_detail="Both access key ID and secret are required",
            )

        # Check structural validity (AWS keys have specific patterns)
        if not access_key_id.startswith("AKIA"):
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AWS,
                message="AWS access key ID has invalid format",
                error_detail="Access key IDs should start with 'AKIA'",
            )

        if len(secret_access_key) < 40:
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AWS,
                message="AWS secret access key appears too short",
                error_detail="AWS secrets are typically 40+ characters",
            )

        # Note: Actual AWS credential testing would require boto3 or
        # making signed requests. This validates structure.
        return CredentialTestResult(
            valid=True,
            credential_type=CredentialType.AWS,
            message="AWS credentials appear structurally valid (format check only)",
            metadata={
                "region": region,
                "note": "Actual AWS API validation requires boto3 or signed requests",
            },
        )

    def test_azure_credentials(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
    ) -> CredentialTestResult:
        """
        Test if Azure credentials are valid.

        Args:
            tenant_id: Azure tenant ID
            client_id: Azure application/client ID
            client_secret: Azure client secret

        Returns:
            CredentialTestResult indicating validity
        """
        if not all([tenant_id, client_id, client_secret]):
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AZURE,
                message="Azure tenant ID, client ID, or client secret is missing",
                error_detail="All three fields are required",
            )

        # Basic UUID validation for tenant_id and client_id
        uuid_pattern = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"

        if not re.match(uuid_pattern, tenant_id, re.IGNORECASE):
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AZURE,
                message="Azure tenant ID has invalid format (not a valid UUID)",
                error_detail=f"Expected UUID format, got: {tenant_id}",
            )

        if not re.match(uuid_pattern, client_id, re.IGNORECASE):
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AZURE,
                message="Azure client ID has invalid format (not a valid UUID)",
                error_detail=f"Expected UUID format, got: {client_id}",
            )

        if len(client_secret) < 20:
            return CredentialTestResult(
                valid=False,
                credential_type=CredentialType.AZURE,
                message="Azure client secret appears too short",
                error_detail="Azure secrets are typically 20+ characters",
            )

        return CredentialTestResult(
            valid=True,
            credential_type=CredentialType.AZURE,
            message="Azure credentials appear structurally valid (format check only)",
            metadata={
                "tenant_id": tenant_id,
                "note": "Actual Azure token validation requires HTTP request to auth endpoint",
            },
        )

    def _test_http_endpoint(
        self,
        endpoint: str,
        headers: dict[str, str] | None = None,
        credential_type: CredentialType = CredentialType.CUSTOM,
    ) -> CredentialTestResult:
        """
        Test credentials by making an HTTP request to an endpoint.

        Args:
            endpoint: The URL to test
            headers: HTTP headers to send
            credential_type: Type of credential being tested

        Returns:
            CredentialTestResult indicating validity
        """
        if not urllib:
            return CredentialTestResult(
                valid=False,
                credential_type=credential_type,
                message="urllib not available",
                error_detail="HTTP testing requires urllib module",
            )

        try:
            req = urllib.request.Request(endpoint, headers=headers or {})
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                if response.status in (200, 401, 403):
                    if response.status == 200:
                        return CredentialTestResult(
                            valid=True,
                            credential_type=credential_type,
                            message="HTTP request successful (200 OK)",
                            metadata={"status_code": 200},
                        )
                    else:
                        return CredentialTestResult(
                            valid=False,
                            credential_type=credential_type,
                            message=f"Authentication failed (HTTP {response.status})",
                            error_detail="Credentials were rejected by the server",
                            metadata={"status_code": response.status},
                        )
                else:
                    return CredentialTestResult(
                        valid=False,
                        credential_type=credential_type,
                        message=f"Unexpected HTTP status: {response.status}",
                        error_detail=f"Expected 200/401/403, got {response.status}",
                        metadata={"status_code": response.status},
                    )
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return CredentialTestResult(
                    valid=False,
                    credential_type=credential_type,
                    message=f"Authentication failed (HTTP {e.code})",
                    error_detail="Credentials were rejected",
                    metadata={"status_code": e.code},
                )
            return CredentialTestResult(
                valid=False,
                credential_type=credential_type,
                message=f"HTTP error: {e.code}",
                error_detail=str(e),
                metadata={"status_code": e.code},
            )
        except urllib.error.URLError as e:
            return CredentialTestResult(
                valid=False,
                credential_type=credential_type,
                message="Failed to reach endpoint",
                error_detail=str(e.reason),
                metadata={"endpoint": endpoint},
            )
        except Exception as e:
            return CredentialTestResult(
                valid=False,
                credential_type=credential_type,
                message="Credential test error",
                error_detail=str(e),
            )

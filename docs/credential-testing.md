# Credential Testing Layer

## Overview

The credential testing layer (`engine/work/credential_tester.py`) is a **local Python utility** for validating that credentials actually work before agents attempt to use them for external operations.

This layer:
- **Does NOT substitute for agent reasoning** — agents decide when/why to test credentials
- **Tests structural validity** — API key format, UUID patterns, field presence
- **Attempts HTTP validation** — makes simple requests to verify credentials work (where applicable)
- **Returns clear results** — success/failure with actionable error messages

## When to Use

The `worker` agent requests credential validation via the `test_credentials` capability,
which the engine executes locally using this module.

**Flow:**
```
worker → requests test_credentials capability
       ↓
engine (local) → runs CredentialTester
              ↓
engine → returns capability result to worker
       ↓
worker → includes validation result in its output
       ↓ (if invalid)
       → reports blocker in output
       ↓ (if valid)
       → marks "credentials validated" and continues work
```

Agents do NOT import this module directly. They request the `test_credentials` capability
and receive structured results. The engine handles execution locally.

## Runtime Capability Interface

Agents request credential testing via the `test_credentials` capability:

```json
{
  "capability": "test_credentials",
  "arguments": {
    "credential_type": "api_key",
    "service": "openai",
    "credentials": {"api_key": "sk-..."}
  },
  "reason": "Validate OpenAI API key before proceeding"
}
```

The engine returns a structured `runtime_capability_result`.

## Internal Implementation

### Testing API Keys

```python
result = tester.test_api_key(
    api_key="sk-...",
    service="openai",  # or "anthropic", "github", "stripe"
    # endpoint="https://custom.example.com/api"  # optional
)

if result.valid:
    print(f"✓ {result.message}")
else:
    print(f"✗ {result.message}: {result.error_detail}")
```

### Testing Bearer Tokens

```python
result = tester.test_bearer_token(
    token="eyJhbGc...",
    endpoint="https://api.example.com/auth/validate"
)

if not result.valid:
    print(result.error_detail)
```

### Testing Basic Auth

```python
result = tester.test_basic_auth(
    username="user@example.com",
    password="password123",
    endpoint="https://api.example.com/auth"
)
```

### Testing AWS Credentials

```python
result = tester.test_aws_credentials(
    access_key_id="AKIAIOSFODNN7EXAMPLE",
    secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    region="us-east-1"
)

# Note: Validates structural format only. Full validation would require boto3.
```

### Testing Azure Credentials

```python
result = tester.test_azure_credentials(
    tenant_id="12345678-1234-1234-1234-123456789012",
    client_id="87654321-4321-4321-4321-210987654321",
    client_secret="a_long_secret_string"
)

# Note: Validates UUID format and secret length. Full token validation requires HTTP request.
```

## Result Format

All test methods return a `CredentialTestResult`:

```python
@dataclass
class CredentialTestResult:
    valid: bool                              # True if credentials appear valid
    credential_type: CredentialType          # Type tested (api_key, bearer_token, etc.)
    message: str                             # Human-readable summary
    error_detail: str | None                 # Specific failure reason (if applicable)
    metadata: dict[str, Any] | None          # Additional context (status codes, etc.)

    def to_dict(self) -> dict[str, Any]:
        # Converts to JSON-serializable dict for artifact persistence
```

## Agent Integration Pattern

Agents request credential validation via the runtime capability system, not by
importing Python directly. See the Runtime Capability Interface section above.

## Limitations

- **HTTP testing only** — requires endpoint accessibility; cannot test offline
- **Format validation** — some providers (AWS, Azure) do structural checks; full validation may require additional libraries (boto3, azure-cli)
- **No retry logic** — single attempt; temporary network failures will appear as credential failures
- **Timeout** — defaults to 10 seconds; very slow services may time out incorrectly

## Design Rationale

This layer exists to:
1. **Fail fast** — catch invalid credentials before agents spend time on failed API calls
2. **Provide clarity** — specific error messages (format mismatch vs. rejected auth) aid user feedback
3. **Stay local** — credentials are validated locally without spawning additional agents
4. **Support agents** — agents decide reasoning; credentials are just one input to that reasoning

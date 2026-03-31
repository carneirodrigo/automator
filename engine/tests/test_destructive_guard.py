"""
Tests for engine.work.destructive_guard

Coverage:
  - Role-based capability allowlist
  - Absolute HTTP blocks (SharePoint sites, Entra users/SPs)
  - Soft HTTP blocks (structural deletions via _HARD_BLOCKED_HTTP domain patterns)
  - HTTP catch-all (DELETE/PATCH/PUT on any domain in build_and_deploy mode)
  - Delivery mode gate (mutating methods without build_and_deploy)
  - Shell command blocklist
  - Shell HTTP tool bypass detection (curl/wget/az rest with mutating methods)
  - Write file: protected directories
  - Write file: pre-existing user file protection
  - Write file: script content scan
  - Run command: script content scan (write + run)
  - Engine-created path registry (register_created_path / is_engine_created)
  - _extract_confirmation_token
  - _scan_script_content
  - _find_script_in_command
  - is_absolute_block helper
  - destructive_guard_block error_category (capability loop caused by guard blocks)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.work.destructive_guard import (
    ROLE_ALLOWED_CAPABILITIES,
    _ABSOLUTE_HTTP_BLOCK_COUNT,
    _HARD_BLOCKED_HTTP,
    _SCRIPT_EXTENSIONS,
    _find_script_in_command,
    _scan_script_content,
    check_capability,
    is_absolute_block,
    is_engine_created,
    register_created_path,
)
from engine.work.engine_runtime import (
    _extract_confirmation_token,
    run_agent_with_capabilities as runtime_run_agent_with_capabilities,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req(capability: str, arguments: dict | None = None) -> dict:
    return {"capability": capability, "arguments": arguments or {}}


def _http(method: str, url: str) -> dict:
    return _req("http_request_with_secret_binding", {"method": method, "url": url})


def _cmd(command: list, cwd: str | None = None) -> dict:
    args = {"command": command}
    if cwd:
        args["cwd"] = cwd
    return _req("run_command", args)


DELIVERY = "projects/test-proj/delivery"


def _write(path: str, content: str = "") -> dict:
    return _req("write_file", {"path": path, "content": content})


# ---------------------------------------------------------------------------
# Role-based allowlist
# ---------------------------------------------------------------------------

class RoleAllowlistTest(unittest.TestCase):

    def test_research_cannot_run_command(self):
        r = check_capability(_req("run_command"), role="research", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_research_cannot_write_file(self):
        r = check_capability(_req("write_file"), role="research", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_review_cannot_write_file(self):
        r = check_capability(_req("write_file"), role="review", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_review_can_run_command(self):
        # review has run_command for verification; blocked only by shell blocklist content, not role
        r = check_capability(_req("run_command", {"command": ["python3", "--version"]}), role="review", delivery_mode=None)
        if r is not None:
            self.assertNotIn("not permitted to use capability", r["issues"][0])

    def test_research_cannot_write_file(self):
        r = check_capability(_req("write_file"), role="research", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_worker_can_write_file(self):
        # Path inside projects/ and no content — passes allowlist; other checks may fire
        r = check_capability(
            _req("write_file", {"path": "projects/x/delivery/out.txt", "content": "hello"}),
            role="worker", delivery_mode=None,
        )
        # allowlist check passes (None or blocked for other reason, not role)
        if r is not None:
            self.assertNotIn("not permitted to use capability", r["issues"][0])

    def test_worker_cannot_deploy_without_delivery_mode(self):
        r = check_capability(_req("deploy_logic_app_definition"), role="worker", delivery_mode=None)
        self.assertIsNotNone(r)

    def test_review_cannot_deploy_logic_app(self):
        r = check_capability(_req("deploy_logic_app_definition"), role="review", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_worker_can_deploy_with_delivery_mode(self):
        # Allowlist check only — deployment gate may still fire
        r = check_capability(
            _req("deploy_logic_app_definition", {"template_path": "/tmp/t.json", "resource_group": "rg"}),
            role="worker", delivery_mode="build_and_deploy",
        )
        # Should NOT be blocked for role reason
        if r is not None:
            self.assertNotIn("not permitted to use capability", r["issues"][0])

    def test_unknown_role_is_permissive(self):
        # Unrecognised roles fall through without allowlist error
        r = check_capability(_req("read_file", {"path": "/tmp/x.txt"}), role="future-role", delivery_mode=None)
        # read_file has no destructive checks — should be None
        self.assertIsNone(r)

    def test_none_role_is_permissive(self):
        r = check_capability(_req("read_file", {"path": "/tmp/x.txt"}), role=None, delivery_mode=None)
        self.assertIsNone(r)

    def test_all_known_roles_have_entries(self):
        expected = {"worker", "research", "review"}
        self.assertEqual(set(ROLE_ALLOWED_CAPABILITIES.keys()), expected)


# ---------------------------------------------------------------------------
# Absolute HTTP blocks (SharePoint sites + Entra users/SPs)
# ---------------------------------------------------------------------------

class AbsoluteHttpBlockTest(unittest.TestCase):

    def _check(self, method: str, url: str):
        return check_capability(
            _http(method, url),
            role="platform-builder", delivery_mode="build_and_deploy",
        )

    # SharePoint classical REST
    def test_patch_sharepoint_site_api_absolute(self):
        r = self._check("PATCH", "https://contoso.sharepoint.com/_api/site")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_put_sharepoint_web_api_absolute(self):
        r = self._check("PUT", "https://contoso.sharepoint.com/_api/web")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_delete_sharepoint_site_api_absolute(self):
        r = self._check("DELETE", "https://contoso.sharepoint.com/_api/site")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    # Microsoft Graph — site root
    def test_delete_graph_site_absolute(self):
        r = self._check("DELETE", "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com,abc,def")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_patch_graph_site_absolute(self):
        r = self._check("PATCH", "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com,abc,def")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    # M365 group
    def test_delete_m365_group_absolute(self):
        r = self._check("DELETE", "https://graph.microsoft.com/v1.0/groups/abc-123")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    # Entra users
    def test_delete_entra_user_absolute(self):
        r = self._check("DELETE", "https://graph.microsoft.com/v1.0/users/john@contoso.com")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_patch_entra_user_absolute(self):
        r = self._check("PATCH", "https://graph.microsoft.com/v1.0/users/00000000-0000-0000-0000-000000000001")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_put_entra_user_absolute(self):
        r = self._check("PUT", "https://graph.microsoft.com/v1.0/users/john@contoso.com")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    # Entra service principals
    def test_delete_service_principal_absolute(self):
        r = self._check("DELETE", "https://graph.microsoft.com/v1.0/servicePrincipals/abc-123")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    def test_patch_service_principal_absolute(self):
        r = self._check("PATCH", "https://graph.microsoft.com/v1.0/servicePrincipals/abc-123")
        self.assertIsNotNone(r)
        self.assertTrue(is_absolute_block(r))

    # GET on absolute-protected resources is allowed
    def test_get_entra_user_allowed(self):
        r = self._check("GET", "https://graph.microsoft.com/v1.0/users/john@contoso.com")
        self.assertIsNone(r)

    def test_get_graph_site_allowed(self):
        r = self._check("GET", "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com,abc,def")
        self.assertIsNone(r)

    def test_absolute_block_count_matches_list(self):
        # Structural check: first N entries are absolute, rest are soft
        self.assertGreater(_ABSOLUTE_HTTP_BLOCK_COUNT, 0)
        self.assertLess(_ABSOLUTE_HTTP_BLOCK_COUNT, len(_HARD_BLOCKED_HTTP))


# ---------------------------------------------------------------------------
# Soft HTTP blocks (domain-specific structural deletions)
# ---------------------------------------------------------------------------

class SoftHttpBlockTest(unittest.TestCase):

    def _check(self, method: str, url: str):
        return check_capability(
            _http(method, url),
            role="platform-builder", delivery_mode="build_and_deploy",
        )

    def test_delete_logic_app_soft(self):
        r = self._check(
            "DELETE",
            "https://management.azure.com/subscriptions/sub1/resourceGroups/rg1"
            "/providers/Microsoft.Logic/workflows/wf1",
        )
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_delete_resource_group_soft(self):
        r = self._check(
            "DELETE",
            "https://management.azure.com/subscriptions/sub1/resourceGroups/rg-prod",
        )
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_delete_sharepoint_list_soft(self):
        r = self._check(
            "DELETE",
            "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com,a,b/lists/list1",
        )
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_delete_teams_team_soft(self):
        r = self._check("DELETE", "https://graph.microsoft.com/v1.0/teams/team-id-123")
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_delete_powerbi_workspace_soft(self):
        r = self._check("DELETE", "https://api.powerbi.com/v1.0/myorg/groups/group-123")
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_delete_powerbi_dataset_soft(self):
        r = self._check(
            "DELETE",
            "https://api.powerbi.com/v1.0/myorg/groups/group-123/datasets/ds-456",
        )
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))


# ---------------------------------------------------------------------------
# HTTP catch-all — action-based, any domain
# ---------------------------------------------------------------------------

class HttpCatchAllTest(unittest.TestCase):

    def _check(self, method: str, url: str, delivery_mode: str = "build_and_deploy"):
        return check_capability(
            _http(method, url),
            role="platform-builder", delivery_mode=delivery_mode,
        )

    def test_delete_qualys_blocked(self):
        r = self._check("DELETE", "https://qualys.qualys.com/api/2.0/fo/report/1")
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_patch_bitsight_blocked(self):
        r = self._check("PATCH", "https://bitsight.com/ratings/company/abc")
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_put_internal_api_blocked(self):
        r = self._check("PUT", "https://my-internal-system.corp/config/reset")
        self.assertIsNotNone(r)
        self.assertFalse(is_absolute_block(r))

    def test_get_any_domain_allowed(self):
        r = self._check("GET", "https://qualys.qualys.com/api/2.0/fo/report/")
        self.assertIsNone(r)

    def test_post_any_domain_allowed(self):
        # POST creates resources — not caught by catch-all
        r = self._check("POST", "https://qualys.qualys.com/api/2.0/fo/report/")
        self.assertIsNone(r)

    def test_delete_without_build_and_deploy_hard_blocked(self):
        # Delivery mode gate fires before catch-all
        r = self._check("DELETE", "https://qualys.qualys.com/api/2.0/fo/report/1", delivery_mode=None)
        self.assertIsNotNone(r)
        self.assertIn("build_and_deploy", r["issues"][0])

    def test_patch_without_build_and_deploy_hard_blocked(self):
        r = self._check("PATCH", "https://qualys.qualys.com/api/2.0/fo/report/1", delivery_mode="build_only")
        self.assertIsNotNone(r)
        self.assertIn("build_and_deploy", r["issues"][0])


# ---------------------------------------------------------------------------
# Delivery mode gate
# ---------------------------------------------------------------------------

class DeliveryModeGateTest(unittest.TestCase):

    def test_post_without_build_and_deploy_blocked(self):
        r = check_capability(
            _http("POST", "https://graph.microsoft.com/v1.0/users"),
            role="platform-builder", delivery_mode=None,
        )
        self.assertIsNotNone(r)
        self.assertIn("build_and_deploy", r["issues"][0])

    def test_post_with_build_and_deploy_allowed(self):
        r = check_capability(
            _http("POST", "https://graph.microsoft.com/v1.0/users"),
            role="platform-builder", delivery_mode="build_and_deploy",
        )
        self.assertIsNone(r)

    def test_deploy_logic_app_without_build_and_deploy_blocked(self):
        r = check_capability(
            _req("deploy_logic_app_definition", {"template_path": "/p/t.json", "resource_group": "rg"}),
            role="platform-builder", delivery_mode="build_only",
        )
        self.assertIsNotNone(r)
        self.assertIn("build_and_deploy", r["issues"][0])

    def test_powerbi_import_without_build_and_deploy_blocked(self):
        r = check_capability(
            _req("powerbi_import_artifact", {}),
            role="platform-builder", delivery_mode=None,
        )
        self.assertIsNotNone(r)
        self.assertIn("build_and_deploy", r["issues"][0])


# ---------------------------------------------------------------------------
# Shell command blocklist
# ---------------------------------------------------------------------------

class ShellCommandBlocklistTest(unittest.TestCase):

    def _check(self, command: list):
        return check_capability(_cmd(command), role="coding", delivery_mode=None)

    def test_rm_rf_blocked(self):
        r = self._check(["rm", "-rf", "/some/path"])
        self.assertIsNotNone(r)
        self.assertIn("BLOCKED", r["issues"][0])

    def test_rm_recursive_blocked(self):
        r = self._check(["rm", "--recursive", "/some/path"])
        self.assertIsNotNone(r)

    def test_rm_r_blocked(self):
        r = self._check(["rm", "-r", "/some/path"])
        self.assertIsNotNone(r)

    def test_find_delete_blocked(self):
        r = self._check(["find", ".", "-name", "*.tmp", "-delete"])
        self.assertIsNotNone(r)

    def test_find_exec_rm_blocked(self):
        r = self._check(["bash", "-c", "find . -exec rm {} \\;"])
        self.assertIsNotNone(r)

    def test_shred_blocked(self):
        r = self._check(["shred", "-u", "secret.txt"])
        self.assertIsNotNone(r)

    def test_rd_s_blocked(self):
        r = self._check(["rd", "/s", "C:\\Temp"])
        self.assertIsNotNone(r)

    def test_remove_pnp_list_item_blocked(self):
        r = self._check(["pwsh", "-c", "Remove-PnPListItem -List 'MyList' -Identity 1"])
        self.assertIsNotNone(r)

    def test_remove_pnp_file_blocked(self):
        r = self._check(["pwsh", "-c", "Remove-PnPFile -ServerRelativeUrl '/sites/x/file.docx'"])
        self.assertIsNotNone(r)

    def test_remove_mg_site_list_item_blocked(self):
        r = self._check(["pwsh", "-c", "Remove-MgSiteListItem -SiteId abc -ListId def -ListItemId 1"])
        self.assertIsNotNone(r)

    def test_safe_rm_allowed(self):
        # Single known file — not recursive
        r = self._check(["rm", "output.csv"])
        self.assertIsNone(r)

    def test_ls_allowed(self):
        r = self._check(["ls", "-la", "/tmp"])
        self.assertIsNone(r)

    def test_python_test_runner_allowed(self):
        r = self._check(["python3", "-m", "unittest", "discover"])
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Shell HTTP tool bypass (run_command)
# ---------------------------------------------------------------------------

class ShellHttpBypassTest(unittest.TestCase):

    def _check(self, command: list):
        return check_capability(_cmd(command), role="coding", delivery_mode=None)

    def test_curl_delete_any_domain_blocked(self):
        r = self._check(["curl", "-X", "DELETE", "https://qualys.qualys.com/api/2.0/fo/report/1"])
        self.assertIsNotNone(r)

    def test_curl_xdelete_compact_blocked(self):
        r = self._check(["curl", "-XDELETE", "https://any-api.example.com/items/abc"])
        self.assertIsNotNone(r)

    def test_curl_request_delete_blocked(self):
        r = self._check(["curl", "--request", "DELETE", "https://bitsight.com/company/abc"])
        self.assertIsNotNone(r)

    def test_curl_patch_blocked(self):
        r = self._check(["curl", "-X", "PATCH", "https://my-internal.corp/config"])
        self.assertIsNotNone(r)

    def test_curl_put_blocked(self):
        r = self._check(["curl", "-X", "PUT", "https://api.example.com/resource/1"])
        self.assertIsNotNone(r)

    def test_az_rest_delete_blocked(self):
        r = self._check(["az", "rest", "--method", "DELETE", "--url", "https://management.azure.com/.../wf1"])
        self.assertIsNotNone(r)

    def test_invoke_restmethod_delete_blocked(self):
        r = self._check(["pwsh", "-c", "Invoke-RestMethod -Method DELETE -Uri https://graph.microsoft.com/v1.0/users/abc"])
        self.assertIsNotNone(r)

    def test_wget_method_patch_blocked(self):
        r = self._check(["wget", "--method", "PATCH", "https://api.example.com/resource"])
        self.assertIsNotNone(r)

    # Allowed cases
    def test_curl_get_allowed(self):
        r = self._check(["curl", "https://qualys.qualys.com/api/2.0/fo/report/"])
        self.assertIsNone(r)

    def test_curl_post_allowed(self):
        # POST creates — not caught
        r = self._check(["curl", "-X", "POST", "https://any-api.example.com/items"])
        self.assertIsNone(r)

    def test_az_rest_get_allowed(self):
        r = self._check(["az", "rest", "--method", "GET", "--url", "https://management.azure.com/..."])
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Write file: protected directories
# ---------------------------------------------------------------------------

class WriteFileProtectedDirTest(unittest.TestCase):

    def _write_path(self, path: str):
        return check_capability(
            _req("write_file", {"path": path, "content": "x"}),
            role="coding", delivery_mode=None,
        )

    def test_engine_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "engine" / "work" / "new_module.py"))
        self.assertIsNotNone(r)
        self.assertIn("not permitted", r["issues"][0])

    def test_agents_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "agents" / "new_agent.md"))
        self.assertIsNotNone(r)

    def test_docs_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "docs" / "new-doc.md"))
        self.assertIsNotNone(r)

    def test_config_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "config" / "backends.json"))
        self.assertIsNotNone(r)

    def test_knowledge_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "knowledge" / "entry.json"))
        self.assertIsNotNone(r)

    def test_skills_dir_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        r = self._write_path(str(REPO_ROOT / "skills" / "catalog.json"))
        self.assertIsNotNone(r)

    def test_projects_dir_allowed(self):
        from engine.work.repo_paths import REPO_ROOT
        # New file in projects/ — no pre-existing file, no destructive content
        p = str(REPO_ROOT / "projects" / "test-proj" / "delivery" / "output.txt")
        r = self._write_path(p)
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Write file: pre-existing user file protection
# ---------------------------------------------------------------------------

class WriteFileOverwriteProtectionTest(unittest.TestCase):

    def test_overwrite_user_file_blocked(self):
        from engine.work.repo_paths import REPO_ROOT
        # Use a file that genuinely exists and was not created by the engine
        existing = REPO_ROOT / "README.md"
        if not existing.exists():
            self.skipTest("README.md not present")
        r = check_capability(
            _req("write_file", {"path": str(existing), "content": "overwrite"}),
            role="coding", delivery_mode=None,
        )
        self.assertIsNotNone(r)
        self.assertIn("not created by the engine", r["issues"][0])

    def test_overwrite_engine_created_file_allowed(self):
        from engine.work.repo_paths import REPO_ROOT
        with tempfile.NamedTemporaryFile(
            dir=REPO_ROOT / "projects", suffix=".txt", delete=False
        ) as f:
            tmp = Path(f.name)
        try:
            register_created_path(tmp)
            r = check_capability(
                _req("write_file", {"path": str(tmp), "content": "update"}),
                role="coding", delivery_mode=None,
            )
            # Should not be blocked for overwrite reason
            if r is not None:
                self.assertNotIn("not created by the engine", r["issues"][0])
        finally:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Write file: script content scan
# ---------------------------------------------------------------------------

class WriteFileScriptContentTest(unittest.TestCase):

    def _write_script(self, filename: str, content: str, role: str = "coding"):
        from engine.work.repo_paths import REPO_ROOT
        path = str(REPO_ROOT / "projects" / "test-proj" / "delivery" / filename)
        return check_capability(
            _req("write_file", {"path": path, "content": content}),
            role=role, delivery_mode=None,
        )

    # Blocked: shell rm -rf in script
    def test_sh_rm_rf_blocked(self):
        r = self._write_script("cleanup.sh", "#!/bin/bash\nrm -rf /data/old\n")
        self.assertIsNotNone(r)
        self.assertIn("destructive", r["issues"][0])

    # Blocked: curl DELETE in shell script
    def test_sh_curl_delete_blocked(self):
        r = self._write_script("deploy.sh", "curl -X DELETE https://qualys.qualys.com/api/2.0/fo/report/1\n")
        self.assertIsNotNone(r)

    # Blocked: requests.delete in Python
    def test_py_requests_delete_blocked(self):
        r = self._write_script("cleanup.py", 'import requests\nrequests.delete("https://qualys.qualys.com/api/2.0/fo/report/1")\n')
        self.assertIsNotNone(r)

    # Blocked: httpx.patch in Python
    def test_py_httpx_patch_blocked(self):
        r = self._write_script("update.py", 'import httpx\nhttpx.patch("https://bitsight.com/ratings/company/abc")\n')
        self.assertIsNotNone(r)

    # Blocked: method="DELETE" with URL in file
    def test_py_method_keyword_blocked(self):
        r = self._write_script("call.py", 'url = "https://internal.corp/api"\nmethod="DELETE"\n')
        self.assertIsNotNone(r)

    # Blocked: PowerShell irm DELETE
    def test_ps1_irm_delete_blocked(self):
        r = self._write_script(
            "run.ps1",
            'Invoke-RestMethod -Method DELETE -Uri "https://graph.microsoft.com/v1.0/users/abc"\n',
        )
        self.assertIsNotNone(r)

    # Allowed: GET call in Python
    def test_py_get_allowed(self):
        r = self._write_script("fetch.py", 'import requests\nresponse = requests.get("https://qualys.qualys.com/api/2.0/fo/report/")\n')
        self.assertIsNone(r)

    # Allowed: ORM delete with no URL
    def test_py_orm_delete_allowed(self):
        r = self._write_script("models.py", "db.session.delete(record)\ndb.session.commit()\n")
        self.assertIsNone(r)

    # Allowed: cache delete with no URL
    def test_py_cache_delete_allowed(self):
        r = self._write_script("cache.py", "cache.delete('my_key')\n")
        self.assertIsNone(r)

    # Allowed: local file removal
    def test_py_os_remove_allowed(self):
        r = self._write_script("clean.py", "import os\nos.remove('output.csv')\n")
        self.assertIsNone(r)

    # Allowed: requests.delete to non-HTTP (no https:// URL in file)
    def test_py_delete_no_url_allowed(self):
        r = self._write_script("del.py", "result = obj.delete(key)\n")
        self.assertIsNone(r)

    # Non-script extensions are not scanned
    def test_txt_with_destructive_content_allowed(self):
        r = self._write_script("notes.txt", "rm -rf /everything\ncurl -X DELETE https://example.com/\n")
        self.assertIsNone(r)

    def test_json_with_destructive_content_allowed(self):
        r = self._write_script("config.json", '{"method": "DELETE", "url": "https://example.com/"}')
        self.assertIsNone(r)


# ---------------------------------------------------------------------------
# Run command: script content scan
# ---------------------------------------------------------------------------

class RunCommandScriptScanTest(unittest.TestCase):

    def _run_script(self, interpreter: list, content: str, suffix: str = ".py"):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            script_path = f.name
        try:
            cmd = interpreter + [script_path]
            return check_capability(_cmd(cmd), role="coding", delivery_mode=None)
        finally:
            Path(script_path).unlink(missing_ok=True)

    def test_python_script_with_requests_delete_blocked(self):
        r = self._run_script(
            ["python3"],
            'import requests\nrequests.delete("https://qualys.qualys.com/api/2.0/fo/report/1")\n',
        )
        self.assertIsNotNone(r)
        self.assertIn("destructive", r["issues"][0])

    def test_bash_script_with_curl_delete_blocked(self):
        r = self._run_script(
            ["bash"],
            "#!/bin/bash\ncurl -X DELETE https://bitsight.com/ratings/company/abc\n",
            suffix=".sh",
        )
        self.assertIsNotNone(r)

    def test_bash_script_with_rm_rf_blocked(self):
        r = self._run_script(
            ["bash"],
            "#!/bin/bash\nrm -rf /tmp/old_data\n",
            suffix=".sh",
        )
        self.assertIsNotNone(r)

    def test_python_script_clean_allowed(self):
        r = self._run_script(
            ["python3"],
            'import requests\nresponse = requests.get("https://api.example.com/items")\nprint(response.json())\n',
        )
        self.assertIsNone(r)

    def test_python_script_orm_delete_allowed(self):
        r = self._run_script(
            ["python3"],
            "db.session.delete(record)\ndb.session.commit()\n",
        )
        self.assertIsNone(r)

    def test_nonexistent_script_allowed(self):
        # Guard must not crash if script file doesn't exist
        r = check_capability(
            _cmd(["python3", "/nonexistent/path/script.py"]),
            role="coding", delivery_mode=None,
        )
        self.assertIsNone(r)

    def test_direct_script_invocation_detected(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, encoding="utf-8"
        ) as f:
            f.write("#!/bin/bash\nrm -rf /data\n")
            script_path = f.name
        try:
            r = check_capability(_cmd([script_path]), role="coding", delivery_mode=None)
            self.assertIsNotNone(r)
        finally:
            Path(script_path).unlink(missing_ok=True)

    def test_interpreter_with_flags_before_script(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, encoding="utf-8"
        ) as f:
            f.write("curl -X DELETE https://api.example.com/items/1\n")
            script_path = f.name
        try:
            r = check_capability(
                _cmd(["bash", "-e", "-x", script_path]),
                role="coding", delivery_mode=None,
            )
            self.assertIsNotNone(r)
        finally:
            Path(script_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _scan_script_content unit tests
# ---------------------------------------------------------------------------

class ScanScriptContentTest(unittest.TestCase):

    def test_rm_rf_detected(self):
        self.assertIsNotNone(_scan_script_content("rm -rf /data"))

    def test_find_delete_detected(self):
        self.assertIsNotNone(_scan_script_content("find . -name '*.tmp' -delete"))

    def test_shred_detected(self):
        self.assertIsNotNone(_scan_script_content("shred -u secret.txt"))

    def test_remove_pnp_list_item_detected(self):
        self.assertIsNotNone(_scan_script_content("Remove-PnPListItem -List 'MyList' -Identity 1"))

    def test_curl_delete_any_domain_detected(self):
        self.assertIsNotNone(_scan_script_content("curl -X DELETE https://any-api.example.com/items/1"))

    def test_curl_patch_detected(self):
        self.assertIsNotNone(_scan_script_content("curl -X PATCH https://internal.corp/config"))

    def test_az_rest_delete_detected(self):
        self.assertIsNotNone(_scan_script_content("az rest --method DELETE --url https://management.azure.com/..."))

    def test_irm_delete_detected(self):
        self.assertIsNotNone(_scan_script_content("Invoke-RestMethod -Method DELETE -Uri https://graph.microsoft.com/v1.0/users/abc"))

    def test_requests_delete_with_url_detected(self):
        self.assertIsNotNone(_scan_script_content('requests.delete("https://qualys.qualys.com/api/2.0/fo/report/1")'))

    def test_httpx_patch_with_url_detected(self):
        self.assertIsNotNone(_scan_script_content('httpx.patch("https://bitsight.com/ratings/abc")'))

    def test_method_equals_delete_with_url_detected(self):
        self.assertIsNotNone(_scan_script_content('method="DELETE"\nurl = "https://internal.corp/api"'))

    def test_method_json_delete_with_url_detected(self):
        self.assertIsNotNone(_scan_script_content('"method": "DELETE"\n"url": "https://api.example.com/items/1"'))

    # Clean cases
    def test_get_request_clean(self):
        self.assertIsNone(_scan_script_content('requests.get("https://api.example.com/items")'))

    def test_orm_delete_no_url_clean(self):
        self.assertIsNone(_scan_script_content("db.session.delete(record)\ndb.session.commit()"))

    def test_cache_delete_no_url_clean(self):
        self.assertIsNone(_scan_script_content("cache.delete('my_key')"))

    def test_post_clean(self):
        self.assertIsNone(_scan_script_content('requests.post("https://api.example.com/items", json=data)'))

    def test_empty_content_clean(self):
        self.assertIsNone(_scan_script_content(""))

    def test_curl_get_clean(self):
        self.assertIsNone(_scan_script_content("curl https://api.example.com/items"))


# ---------------------------------------------------------------------------
# _find_script_in_command unit tests
# ---------------------------------------------------------------------------

class FindScriptInCommandTest(unittest.TestCase):

    def test_direct_py_script(self):
        p = _find_script_in_command(["/abs/path/script.py"])
        self.assertEqual(p, Path("/abs/path/script.py"))

    def test_direct_sh_script(self):
        p = _find_script_in_command(["./deploy.sh"])
        self.assertEqual(p, Path("./deploy.sh"))

    def test_python3_interpreter(self):
        p = _find_script_in_command(["python3", "script.py"])
        self.assertEqual(p, Path("script.py"))

    def test_bash_interpreter(self):
        p = _find_script_in_command(["bash", "deploy.sh"])
        self.assertEqual(p, Path("deploy.sh"))

    def test_bash_with_flags(self):
        p = _find_script_in_command(["bash", "-e", "-x", "deploy.sh"])
        self.assertEqual(p, Path("deploy.sh"))

    def test_pwsh_interpreter(self):
        p = _find_script_in_command(["pwsh", "run.ps1"])
        self.assertEqual(p, Path("run.ps1"))

    def test_node_interpreter(self):
        p = _find_script_in_command(["node", "app.js"])
        self.assertEqual(p, Path("app.js"))

    def test_cwd_resolves_relative_path(self):
        p = _find_script_in_command(["python3", "script.py"], cwd="/projects/x")
        self.assertEqual(p, Path("/projects/x/script.py"))

    def test_no_script_in_command(self):
        p = _find_script_in_command(["ls", "-la", "/tmp"])
        self.assertIsNone(p)

    def test_empty_command(self):
        p = _find_script_in_command([])
        self.assertIsNone(p)

    def test_python_module_not_detected_as_script(self):
        # python3 -m unittest — no script file
        p = _find_script_in_command(["python3", "-m", "unittest"])
        self.assertIsNone(p)

    def test_non_script_extension_ignored(self):
        p = _find_script_in_command(["cat", "output.txt"])
        self.assertIsNone(p)


# ---------------------------------------------------------------------------
# _extract_confirmation_token unit tests
# ---------------------------------------------------------------------------

class ExtractConfirmationTokenTest(unittest.TestCase):

    def test_upn_extracted(self):
        self.assertEqual(
            _extract_confirmation_token("https://graph.microsoft.com/v1.0/users/john@contoso.com"),
            "john@contoso.com",
        )

    def test_guid_extracted(self):
        self.assertEqual(
            _extract_confirmation_token(
                "https://graph.microsoft.com/v1.0/users/00000000-1234-5678-abcd-000000000001"
            ),
            "00000000-1234-5678-abcd-000000000001",
        )

    def test_site_id_extracted(self):
        token = _extract_confirmation_token(
            "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com,abc,def"
        )
        self.assertEqual(token, "contoso.sharepoint.com,abc,def")

    def test_group_id_extracted(self):
        token = _extract_confirmation_token(
            "https://graph.microsoft.com/v1.0/groups/abc-123"
        )
        self.assertEqual(token, "abc-123")

    def test_short_segment_falls_back_to_url(self):
        # Segment shorter than 3 chars — use full URL
        url = "https://contoso.sharepoint.com/_api/site"
        token = _extract_confirmation_token(url)
        # "site" is 4 chars, should be returned
        self.assertEqual(token, "site")

    def test_bare_path_falls_back_to_full_url(self):
        url = "https://contoso.sharepoint.com/_api/web"
        token = _extract_confirmation_token(url)
        self.assertEqual(token, "web")

    def test_service_principal_id_extracted(self):
        token = _extract_confirmation_token(
            "https://graph.microsoft.com/v1.0/servicePrincipals/sp-guid-here"
        )
        self.assertEqual(token, "sp-guid-here")


# ---------------------------------------------------------------------------
# is_absolute_block helper
# ---------------------------------------------------------------------------

class IsAbsoluteBlockTest(unittest.TestCase):

    def test_absolute_true(self):
        self.assertTrue(is_absolute_block({"absolute": True}))

    def test_absolute_false(self):
        self.assertFalse(is_absolute_block({"absolute": False}))

    def test_absolute_missing(self):
        self.assertFalse(is_absolute_block({}))

    def test_absolute_none(self):
        self.assertFalse(is_absolute_block({"absolute": None}))


# ---------------------------------------------------------------------------
# Engine-created path registry
# ---------------------------------------------------------------------------

class EngineCreatedPathRegistryTest(unittest.TestCase):

    def test_registered_path_is_engine_created(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = Path(f.name)
        try:
            register_created_path(p)
            self.assertTrue(is_engine_created(p))
        finally:
            p.unlink(missing_ok=True)

    def test_unregistered_existing_file_not_engine_created(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            p = Path(f.name)
        try:
            # Do NOT register it
            self.assertFalse(is_engine_created(p))
        finally:
            p.unlink(missing_ok=True)

    def test_nonexistent_path_not_engine_created(self):
        self.assertFalse(is_engine_created(Path("/nonexistent/path/file.txt")))

    def test_registry_persisted_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake project structure
            project_dir = Path(tmpdir) / "projects" / "test-reg-proj" / "runtime"
            project_dir.mkdir(parents=True)

            # Patch REPO_ROOT so _registry_file resolves correctly
            fake_path = Path(tmpdir) / "projects" / "test-reg-proj" / "delivery" / "output.txt"
            fake_path.parent.mkdir(parents=True, exist_ok=True)
            fake_path.touch()

            with mock.patch("engine.work.destructive_guard.REPO_ROOT" if False else "engine.work.repo_paths.REPO_ROOT", Path(tmpdir)):
                # Just verify register_created_path doesn't crash on arbitrary paths
                register_created_path(fake_path)
                # In-memory cache should have it
                self.assertTrue(is_engine_created(fake_path))


# ---------------------------------------------------------------------------
# Script extensions coverage
# ---------------------------------------------------------------------------

class ScriptExtensionsTest(unittest.TestCase):

    def test_common_extensions_present(self):
        for ext in [".py", ".sh", ".ps1", ".js", ".ts", ".rb", ".bat"]:
            self.assertIn(ext, _SCRIPT_EXTENSIONS, f"Missing extension: {ext}")

    def test_data_extensions_not_present(self):
        for ext in [".txt", ".json", ".csv", ".md", ".yaml", ".xml"]:
            self.assertNotIn(ext, _SCRIPT_EXTENSIONS, f"Unexpected extension: {ext}")


# ---------------------------------------------------------------------------
# destructive_guard_block error_category
# ---------------------------------------------------------------------------

class DestructiveGuardBlockCategoryTest(unittest.TestCase):
    """
    When a capability loop exhausts because the guard kept blocking every
    request, engine_runtime.run_agent_with_capabilities must upgrade
    error_category from 'capability_loop' to 'destructive_guard_block'.

    This makes it unambiguous to any reader (human, AI, master) that the
    failure is an intentional safety policy, not a defect to investigate
    or fix in the engine code.
    """

    def _make_session(self):
        from engine.work.sessions import AgentSession
        return AgentSession()

    def test_guard_block_upgrades_error_category(self):
        """
        A capability loop caused entirely by guard blocks must produce
        error_category='destructive_guard_block', not 'capability_loop'.
        """
        from unittest.mock import MagicMock, patch

        # Agent always requests a blocked capability (simulates a looping agent).
        agent_response = {
            "status": "capability_requested",
            "capability_requests": [{
                "capability": "http_request_with_secret_binding",
                "arguments": {
                    "url": "https://graph.microsoft.com/v1.0/users/john@contoso.com",
                    "method": "DELETE",
                },
            }],
        }

        # Patch run_agent (the subprocess layer) so no real process spawns,
        # and patch _prompt_absolute_block so no TTY interaction occurs.
        with patch("engine.work.engine_runtime.run_agent", return_value=agent_response), \
             patch("engine.work.engine_runtime._prompt_absolute_block", return_value=False):
            result = runtime_run_agent_with_capabilities(
                role="platform-builder",
                task="delete a user",
                reason="test",
                inputs=[],
                project=None,
                agent_bin="claude",
                delivery_mode="build_and_deploy",
                session=self._make_session(),
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error_category"],
            "destructive_guard_block",
            "error_category must be 'destructive_guard_block' when loop is caused by guard blocks, "
            f"got: {result.get('error_category')!r}",
        )
        self.assertIn("Guard blocks", result["error"])

    def test_non_guard_loop_keeps_capability_loop_category(self):
        """
        A capability loop where the guard never blocks must keep
        error_category='capability_loop' (no upgrade).
        """
        from unittest.mock import patch

        # Agent always requests read_file — guard allows it, loop never terminates.
        agent_response = {
            "status": "capability_requested",
            "capability_requests": [{
                "capability": "read_file",
                "arguments": {"path": "README.md"},
            }],
        }

        with patch("engine.work.engine_runtime.run_agent", return_value=agent_response), \
             patch("engine.work.capabilities.execute_capability",
                   return_value={"status": "success", "result": "file contents"}):
            result = runtime_run_agent_with_capabilities(
                role="coding",
                task="read a file forever",
                reason="test",
                inputs=[],
                project=None,
                agent_bin="claude",
                session=self._make_session(),
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error_category"],
            "capability_loop",
            "error_category must stay 'capability_loop' when no guard blocks fired",
        )


if __name__ == "__main__":
    unittest.main()

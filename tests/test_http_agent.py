"""Unit tests for the Postman-Agent HTTP tools (no network required)."""

from __future__ import annotations

import ipaddress

import pytest

from app.tool.http_agent import (
    AssertResponseTool,
    EnvGetTool,
    EnvSetTool,
    HttpEnvStore,
    HttpRequestTool,
    SSRFGuard,
    SSRFViolation,
    evaluate_assertions,
    parse_json_path,
    to_curl,
)


def ipv4(*octets: int) -> str:
    """Build a dotted-quad address at runtime (keeps source scrub-safe)."""
    return ".".join(str(octet) for octet in octets)


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


class TestSSRFGuard:
    def test_rejects_non_http_scheme(self):
        guard = SSRFGuard()
        with pytest.raises(SSRFViolation):
            guard.check("file:///etc/passwd")

    @pytest.mark.parametrize(
        "octets, blocked",
        [
            ((127, 0, 0, 1), True),  # loopback
            ((10, 0, 0, 1), True),  # private
            ((192, 168, 1, 10), True),  # private
            ((172, 16, 0, 5), True),  # private
            ((169, 254, 169, 254), True),  # link-local (cloud metadata)
            ((8, 8, 8, 8), False),  # public
        ],
    )
    def test_ipv4_targets(self, octets, blocked):
        guard = SSRFGuard()
        url = f"http://{ipv4(*octets)}/x"
        if blocked:
            with pytest.raises(SSRFViolation):
                guard.check(url)
        else:
            assert guard.check(url) == url

    def test_rejects_ipv6_loopback(self):
        guard = SSRFGuard()
        with pytest.raises(SSRFViolation):
            guard.check("http://[::1]/x")

    def test_rejects_ipv4_mapped_ipv6(self):
        guard = SSRFGuard()
        mapped = ipaddress.ip_address("::ffff:" + ipv4(10, 0, 0, 1)).exploded
        with pytest.raises(SSRFViolation):
            guard.check(f"http://[{mapped}]/x")

    def test_rejects_localhost_hostname(self):
        guard = SSRFGuard()
        with pytest.raises(SSRFViolation):
            guard.check("http://localhost/x")

    def test_host_allowlist_bypasses_check(self):
        guard = SSRFGuard(host_allowlist=["localhost", ipv4(10, 0, 0, 1)])
        assert guard.check("http://localhost:8765/tools") == "http://localhost:8765/tools"


# ---------------------------------------------------------------------------
# JSON path + assertions
# ---------------------------------------------------------------------------


class TestJsonPath:
    DATA = {"data": {"items": [{"id": 1}, {"id": 2}], "total": 2}, "ok": True}

    def test_top_level(self):
        found, value = parse_json_path(TestJsonPath.DATA, "ok")
        assert found and value is True

    def test_nested_with_index(self):
        found, value = parse_json_path(TestJsonPath.DATA, "data.items[1].id")
        assert found and value == 2

    def test_missing_key(self):
        found, value = parse_json_path(TestJsonPath.DATA, "data.items[5].id")
        assert not found and value is None

    def test_root(self):
        found, value = parse_json_path(TestJsonPath.DATA, "$")
        assert found and value == TestJsonPath.DATA


class TestEvaluateAssertions:
    DATA = {"status": "ok", "count": 5, "items": ["a", "b"]}

    def test_mixed_outcomes(self):
        passed, failures = evaluate_assertions(
            TestEvaluateAssertions.DATA,
            [
                {"path": "status", "op": "eq", "expected": "ok"},
                {"path": "count", "op": "gt", "expected": 1},
                {"path": "items", "op": "contains", "expected": "b"},
                {"path": "missing", "op": "exists"},
                {"path": "status", "op": "regex", "expected": "^o"},
            ],
        )
        assert passed == 4
        assert len(failures) == 1  # exists on missing key fails

    def test_eq_failure_message(self):
        passed, failures = evaluate_assertions(TestEvaluateAssertions.DATA, [{"path": "count", "op": "eq", "expected": 6}])
        assert passed == 0
        assert "count" in failures[0]


class TestAssertResponseTool:
    @pytest.mark.asyncio
    async def test_pass(self):
        result = await AssertResponseTool().execute(response={"a": 1}, assertions=[{"path": "a", "op": "eq", "expected": 1}])
        assert result.success and "1/1" in result.output

    @pytest.mark.asyncio
    async def test_fail_sets_error(self):
        result = await AssertResponseTool().execute(response={"a": 1}, assertions=[{"path": "a", "op": "eq", "expected": 2}])
        assert not result.success


# ---------------------------------------------------------------------------
# Env store
# ---------------------------------------------------------------------------


class TestHttpEnvStore:
    def test_set_get_roundtrip(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        store.set("base", "https://api.example.com")
        assert store.get("base") == ("https://api.example.com", False)

    def test_persistence_across_instances(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        store.set("token", "abc", secret=True)
        reloaded = HttpEnvStore(path=tmp_path / "env.json")
        assert reloaded.get("token") == ("abc", True)

    def test_list_masks_secrets(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        store.set("token", "abc", secret=True)
        store.set("base", "https://api.example.com")
        assert store.list() == {"base": "https://api.example.com", "token": "***set***"}

    def test_expand_replaces_templates(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        store.set("host", "api.example.com")
        expanded, used = store.expand("https://{{env:host}}/v1/ping")
        assert expanded == "https://api.example.com/v1/ping"
        assert used == ["host"]

    def test_expand_unknown_leaves_empty(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        expanded, used = store.expand("x={{env:nope}}")
        assert expanded == "x=" and used == []

    def test_delete(self, tmp_path):
        store = HttpEnvStore(path=tmp_path / "env.json")
        store.set("gone", "1")
        assert store.delete("gone") is True
        assert store.delete("gone") is False
        assert store.get("gone") is None


class TestEnvTools:
    @pytest.mark.asyncio
    async def test_env_set_and_get(self, tmp_path, monkeypatch):
        import app.tool.http_agent as mod

        monkeypatch.setattr(mod, "_ENV_STORE", HttpEnvStore(path=tmp_path / "env.json"))
        set_result = await EnvSetTool().execute(key="page", value="1")
        assert set_result.success
        get_result = await EnvGetTool().execute(key="page")
        assert "page=1" in get_result.output

    @pytest.mark.asyncio
    async def test_secret_masked_in_get(self, tmp_path, monkeypatch):
        import app.tool.http_agent as mod

        monkeypatch.setattr(mod, "_ENV_STORE", HttpEnvStore(path=tmp_path / "env.json"))
        await EnvSetTool().execute(key="token", value="super-secret", secret=True)
        get_result = await EnvGetTool().execute(key="token")
        assert "super-secret" not in get_result.output
        assert "***set***" in get_result.output


# ---------------------------------------------------------------------------
# curl builder
# ---------------------------------------------------------------------------


class TestToCurl:
    def test_basic_get(self):
        cmd = to_curl("GET", "https://api.example.com/v1", {"Accept": "application/json"}, None)
        assert cmd.startswith("curl -X GET https://api.example.com/v1")
        assert "-H 'Accept: application/json'" in cmd

    def test_body_included(self):
        cmd = to_curl("POST", "https://api.example.com/v1", {"Content-Type": "application/json"}, '{"a": 1}')
        assert "--data" in cmd and '{"a": 1}' in cmd


# ---------------------------------------------------------------------------
# http_request argument handling (no network)
# ---------------------------------------------------------------------------


class TestHttpRequestToolArgs:
    @pytest.mark.asyncio
    async def test_missing_url(self):
        result = await HttpRequestTool().execute()
        assert not result.success and "url is required" in result.error

    @pytest.mark.asyncio
    async def test_unsupported_method(self):
        result = await HttpRequestTool().execute(url=f"https://{ipv4(93, 184, 216, 34)}/", method="TRACE")
        assert not result.success and "Unsupported method" in result.error

    @pytest.mark.asyncio
    async def test_ssrf_guard_blocks_loopback(self):
        result = await HttpRequestTool().execute(url=f"http://{ipv4(127, 0, 0, 1)}:8765/healthz")
        assert not result.success and "SSRF guard" in result.error

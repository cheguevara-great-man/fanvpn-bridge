from __future__ import annotations

import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from fanvpn_bridge.contracts import Header, ResolvedRoute
from fanvpn_bridge.product_cache import ProductResponseCache


def plugin_route(query: str = "scope=GLOBAL&limit=200") -> ResolvedRoute:
    return ResolvedRoute(
        name="chatgpt-backend",
        upstream_base_url="https://chatgpt.com",
        upstream_url=f"https://chatgpt.com/backend-api/ps/plugins/list?{query}",
    )


def product_route(path: str) -> ResolvedRoute:
    return ResolvedRoute(
        name="chatgpt-backend",
        upstream_base_url="https://chatgpt.com",
        upstream_url=f"https://chatgpt.com{path}",
    )


class ProductResponseCacheTests(unittest.TestCase):
    def test_only_authenticated_global_plugin_catalog_gets_are_cacheable(self) -> None:
        cache = ProductResponseCache()
        headers = [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")]
        self.assertIsNotNone(cache.policy("GET", plugin_route(), headers))
        self.assertIsNone(cache.policy("POST", plugin_route(), headers))
        self.assertIsNone(cache.policy("GET", plugin_route("scope=WORKSPACE"), headers))
        self.assertIsNone(cache.policy("GET", plugin_route(), []))

    def test_short_lived_startup_metadata_is_cacheable_but_mutations_are_not(self) -> None:
        cache = ProductResponseCache()
        headers = [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")]
        account = cache.policy(
            "GET",
            product_route("/backend-api/wham/accounts/check"),
            headers,
        )
        installed = cache.policy(
            "GET",
            product_route("/backend-api/ps/plugins/installed?scope=GLOBAL"),
            headers,
        )
        connectors = cache.policy(
            "GET",
            product_route("/backend-api/connectors/directory/list?external_logos=true"),
            headers,
        )
        self.assertIsNotNone(account)
        self.assertIsNotNone(installed)
        self.assertIsNotNone(connectors)
        self.assertLess(account.ttl_seconds, connectors.ttl_seconds)
        self.assertIsNone(
            cache.policy(
                "POST",
                product_route("/backend-api/ps/plugins/installed?scope=GLOBAL"),
                headers,
            )
        )
        self.assertIsNone(
            cache.policy("GET", product_route("/backend-api/wham/usage"), headers)
        )

    def test_entries_are_partitioned_by_account_and_expire(self) -> None:
        cache = ProductResponseCache()
        route = plugin_route()
        policy_a = cache.policy(
            "GET",
            route,
            [Header("Authorization", "Bearer a"), Header("ChatGPT-Account-ID", "acct-a")],
        )
        policy_b = cache.policy(
            "GET",
            route,
            [Header("Authorization", "Bearer b"), Header("ChatGPT-Account-ID", "acct-b")],
        )
        assert policy_a is not None and policy_b is not None
        self.assertNotEqual(policy_a.key, policy_b.key)
        with patch("fanvpn_bridge.product_cache.time.monotonic", return_value=100.0):
            self.assertTrue(
                cache.put(
                    policy_a,
                    status=200,
                    headers=(Header("Content-Type", "application/json"),),
                    body=b'{"plugins":[]}',
                )
            )
        with patch("fanvpn_bridge.product_cache.time.monotonic", return_value=101.0):
            self.assertIsNotNone(cache.get(policy_a))
            self.assertIsNone(cache.get(policy_b))
        with patch(
            "fanvpn_bridge.product_cache.time.monotonic",
            return_value=100.0 + policy_a.ttl_seconds + 1,
        ):
            self.assertIsNone(cache.get(policy_a))

    def test_set_cookie_and_oversized_responses_are_not_cached(self) -> None:
        cache = ProductResponseCache()
        policy = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")],
        )
        assert policy is not None
        self.assertFalse(
            cache.put(
                policy,
                status=200,
                headers=(
                    Header("Content-Type", "application/json"),
                    Header("Set-Cookie", "private=value"),
                ),
                body=b"{}",
            )
        )
        self.assertFalse(
            cache.put(
                policy,
                status=200,
                headers=(Header("Content-Type", "application/json"),),
                body=b"x" * (policy.max_body_bytes + 1),
            )
        )

    def test_response_cache_directives_and_content_type_are_respected(self) -> None:
        cache = ProductResponseCache()
        policy = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer secret")],
        )
        assert policy is not None
        for headers in (
            (Header("Content-Type", "application/json"), Header("Cache-Control", "no-store")),
            (Header("Content-Type", "application/json"), Header("Cache-Control", "no-cache")),
            (Header("Content-Type", "application/json"), Header("Pragma", "no-cache")),
            (Header("Content-Type", "application/json"), Header("Vary", "*")),
            (Header("Content-Type", "text/html"),),
            (),
        ):
            self.assertFalse(cache.put(policy, status=200, headers=headers, body=b"{}"))
        self.assertTrue(
            cache.put(
                policy,
                status=200,
                headers=(
                    Header("Content-Type", "application/problem+json; charset=utf-8"),
                    Header("Vary", "Accept"),
                ),
                body=b"{}",
            )
        )

    def test_cache_key_covers_all_client_header_variants(self) -> None:
        cache = ProductResponseCache()
        common = [Header("Authorization", "Bearer secret")]
        json_policy = cache.policy(
            "GET", plugin_route(), common + [Header("Accept", "application/json")]
        )
        text_policy = cache.policy(
            "GET", plugin_route(), common + [Header("Accept", "text/plain")]
        )
        assert json_policy is not None and text_policy is not None
        self.assertNotEqual(json_policy.key, text_policy.key)

    def test_account_partition_survives_access_token_rotation(self) -> None:
        cache = ProductResponseCache()
        first = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer first"), Header("ChatGPT-Account-ID", "acct")],
        )
        refreshed = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer refreshed"), Header("ChatGPT-Account-ID", "acct")],
        )
        other = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer first"), Header("ChatGPT-Account-ID", "other")],
        )
        assert first is not None and refreshed is not None and other is not None
        self.assertEqual(first.key, refreshed.key)
        self.assertNotEqual(first.key, other.key)

    def test_global_catalog_persists_across_cache_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            headers = [
                Header("Authorization", "Bearer first"),
                Header("ChatGPT-Account-ID", "acct"),
            ]
            first_cache = ProductResponseCache(persistent_directory=path)
            first_policy = first_cache.policy("GET", plugin_route(), headers)
            assert first_policy is not None
            self.assertIsNotNone(first_policy.persistent_ttl_seconds)
            self.assertTrue(
                first_cache.put(
                    first_policy,
                    status=200,
                    headers=(Header("Content-Type", "application/json"),),
                    body=b'{"plugins":[]}',
                )
            )

            second_cache = ProductResponseCache(persistent_directory=path)
            second_policy = second_cache.policy(
                "GET",
                plugin_route(),
                [
                    Header("Authorization", "Bearer refreshed"),
                    Header("ChatGPT-Account-ID", "acct"),
                ],
            )
            assert second_policy is not None
            hit = second_cache.get(second_policy)
            self.assertIsNotNone(hit)
            assert hit is not None
            self.assertEqual(hit[0].body, b'{"plugins":[]}')

    def test_installed_plugin_state_is_never_persisted(self) -> None:
        cache = ProductResponseCache()
        policy = cache.policy(
            "GET",
            product_route("/backend-api/ps/plugins/installed?scope=GLOBAL"),
            [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")],
        )
        assert policy is not None
        self.assertIsNone(policy.persistent_ttl_seconds)

    def test_connector_directory_is_short_lived_and_persistent(self) -> None:
        cache = ProductResponseCache()
        policy = cache.policy(
            "GET",
            product_route("/backend-api/connectors/directory/list?external_logos=true"),
            [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")],
        )
        assert policy is not None
        self.assertEqual(policy.persistent_ttl_seconds, 60 * 60)

    def test_identical_cache_misses_share_one_in_flight_owner(self) -> None:
        cache = ProductResponseCache()
        policy = cache.policy(
            "GET",
            plugin_route(),
            [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")],
        )
        assert policy is not None

        owner = cache.acquire(policy)
        waiter = cache.acquire(policy)
        self.assertTrue(owner.owner)
        self.assertFalse(waiter.owner)
        self.assertIsNotNone(waiter.wait_event)
        self.assertFalse(waiter.wait_event.is_set())

        self.assertTrue(
            cache.put(
                policy,
                status=200,
                headers=(Header("Content-Type", "application/json"),),
                body=b'{"plugins":[]}',
            )
        )
        cache.complete(policy)
        self.assertTrue(waiter.wait_event.wait(0.1))
        hit = cache.acquire(policy)
        self.assertIsNotNone(hit.cached)
        self.assertFalse(hit.owner)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from fanvpn_bridge.contracts import Header, ResolvedRoute
from fanvpn_bridge.product_cache import ProductResponseCache


def plugin_route(query: str = "scope=GLOBAL&limit=200") -> ResolvedRoute:
    return ResolvedRoute(
        name="chatgpt-backend",
        upstream_base_url="https://chatgpt.com",
        upstream_url=f"https://chatgpt.com/backend-api/ps/plugins/list?{query}",
    )


class ProductResponseCacheTests(unittest.TestCase):
    def test_only_authenticated_global_plugin_catalog_gets_are_cacheable(self) -> None:
        cache = ProductResponseCache()
        headers = [Header("Authorization", "Bearer secret"), Header("ChatGPT-Account-ID", "acct")]
        self.assertIsNotNone(cache.policy("GET", plugin_route(), headers))
        self.assertIsNone(cache.policy("POST", plugin_route(), headers))
        self.assertIsNone(cache.policy("GET", plugin_route("scope=WORKSPACE"), headers))
        self.assertIsNone(cache.policy("GET", plugin_route(), []))

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
                headers=(Header("Set-Cookie", "private=value"),),
                body=b"{}",
            )
        )
        self.assertFalse(
            cache.put(
                policy,
                status=200,
                headers=(),
                body=b"x" * (policy.max_body_bytes + 1),
            )
        )

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

        self.assertTrue(cache.put(policy, status=200, headers=(), body=b'{"plugins":[]}'))
        cache.complete(policy)
        self.assertTrue(waiter.wait_event.wait(0.1))
        hit = cache.acquire(policy)
        self.assertIsNotNone(hit.cached)
        self.assertFalse(hit.owner)


if __name__ == "__main__":
    unittest.main()

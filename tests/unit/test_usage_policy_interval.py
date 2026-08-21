from __future__ import annotations

import unittest

from fanvpn_bridge import usage_reporting


class UsagePolicyIntervalTests(unittest.TestCase):
    def test_policy_sync_interval_is_short_enough_for_allocation_changes(self) -> None:
        self.assertEqual(usage_reporting._QUOTA_SYNC_INTERVAL_SECONDS, 30.0)


if __name__ == "__main__":
    unittest.main()

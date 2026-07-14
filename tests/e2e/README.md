# Real Chrome / FanVPN tests

These tests require the user's Chrome profile and an enabled FanVPN connection.
They are never reported as CI success unless the real browser path was used.

The production path uses Offscreen Document fetch. A Service Worker network
fallback remains disabled because it has not been proven to use the same FanVPN
route.

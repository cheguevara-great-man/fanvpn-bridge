# Real Chrome / FanVPN tests

These tests require the user's Chrome profile and an enabled FanVPN connection.
They are never reported as CI success unless the real browser path was used.

The first A/B test compares Service Worker fetch with Offscreen Document fetch.
If only Offscreen uses the FanVPN route, runtime fallback to direct Service
Worker fetch remains disabled.

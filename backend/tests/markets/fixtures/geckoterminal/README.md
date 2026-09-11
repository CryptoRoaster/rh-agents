# GeckoTerminal contract fixtures

Synthetic test fixtures, not live market observations. No credentials are present.

Contract: https://api.geckoterminal.com/docs/v2/swagger.json, inspected 2026-09-10; public V2, Accept version 20230203. Used schemas: networks_list, pool/pool_resource and pools_included. Network ID/platform associations were checked against public /networks pages 1 and 3 on that date.

networks.json contains the two verified target associations. robinhood_new_pools.json and bsc_new_pools.json use deliberately synthetic nonzero EVM addresses and fixture DEX/symbol metadata with realistic response structure. High-precision numeric strings are regression inputs, not observed prices. Tests additionally encode JSON numeric literals to prove Decimal-safe transport behavior. Creation dates are deliberately unrelated to injected Clock time.

Only MockTransport reads these files in tests. They are not runtime database state and must never be shown as live dashboard data.

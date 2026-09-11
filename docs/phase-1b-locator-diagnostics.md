# Pool locator diagnosis and verification

Read-only structural diagnostics, 2026-09-10. Two new-pools requests, first three examples per network. Addresses are public provider identifiers; full payloads are not retained in documentation.

| Network | DEX | Actual attributes.address | Bytes | Base token | Quote token |
| --- | --- | --- | --- | --- | --- |
| bsc | four-meme | `0xa4c90c2186fec8ff723ca70cdeba9405ca27adae` | 20 | `0xa4c90c2186fec8ff723ca70cdeba9405ca27adae` | `0x0000000000000000000000000000000000000000` |
| bsc | four-meme | `0xddbd3a489e3a79fb34f72d700ad37f9cd1994444` | 20 | `0xddbd3a489e3a79fb34f72d700ad37f9cd1994444` | `0x0000000000000000000000000000000000000000` |
| bsc | uniswap-v4-bsc | `0x3355a33ba2d236dea5f753c3418dac1ea05e48e8d42c6c55acb5283c7029fa32` | 32 | `0x02e75d28a8aa2a0033b8cf866fcf0bb0e1ee4444` | `0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d` |
| robinhood | uniswap-v4-robinhood | `0xcbcc74318d3fdefa9a41cfffb8cc01a60bb6e053d4481c9f58ca6e8a3f6f5a4f` | 32 | `0x107c0f27650d5d6bd63b697e70c72ba7829142cc` | `0x0000000000000000000000000000000000000000` |
| robinhood | pons-v2 | `0x9730aab42936e8331fac441a730ac21f3e650dfa` | 20 | `0x738619820e6a0fee5698302752dc7c76c4d40f44` | `0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` |
| robinhood | uniswap-v4-robinhood | `0x1f403a3f6cee1c689200c1d703b87a3fe0accc36f529ab1afde62873178655e7` | 32 | `0xf75dd062cfedc553718771db2262d2b030c9efee` | `0x0000000000000000000000000000000000000000` |

For every example, data.id is `<provider-network>_<attributes.address>`; it is a JSON:API binding, not the source of the locator. Every displayed locator is nonzero hex: 20-byte entries match exactly 40 hex digits, 32-byte entries exactly 64. The 32-byte values are present in the actual attributes.address field for uniswap-v4-bsc and uniswap-v4-robinhood. This is not an adapter prefix/parsing error. Uniswap documents v4 pools as bytes32 PoolKey-derived IDs within a singleton PoolManager ([PoolManager](https://developers.uniswap.org/docs/protocols/v4/concepts/poolmanager), [v4 queries](https://developers.uniswap.org/docs/ecosystem/subgraphs/concepts/v4/queries)). This establishes the locator interpretation, not independent on-chain attestation of these particular pools. No PoolManager address was independently supplied; it remains UNKNOWN.

Other observed venues were four-meme and pons-v2 with 20-byte locators. Zero token-address placeholders are rejected; they are not guessed to be wrapped native tokens. Nonzero address syntax does not attest deployed contract code.

The first post-fix smoke encountered an independently diagnosed verification-environment fault: the disposable PostgreSQL database used SQL_ASCII and could not store Unicode token symbols in JSONB. BSC discovered=20 recorded=1 failed=7 error=recording_failed readable=1; Robinhood discovered=20 recorded=0 failed=3 error=recording_failed readable=0. Five logical requests/HTTP attempts. The failed count includes rejected provider items and the recording error; the pass stopped recording subsequent pairs after that error. A UTF-8 verification database and Unicode precision regression replace that invalid test setup.


## Earlier UTF-8 locator verification

Native PostgreSQL full suite: **321 passed**, **96% coverage**. SQLite: **311 passed, 10 PostgreSQL-specific skips**. There are 32 additional locator/precision/migration regressions compared with the earlier 289-test suite. Both mocked complete ingestion paths assert discovered=1 recorded=1 failed=0. Precision roundtrips cover both chains with Unicode symbols, 19 and 30-plus fractional places, scientific notation, replay and API output.

Ruff, Ruff formatting, strict mypy (40 source files), Alembic upgrade/downgrade/re-upgrade, schema verification and offline SQL passed. Migration tests verify downgrade preserves version-1 observations, refuses version-2 data and retains append-only protection. Frontend typecheck, lint, formatting and production build passed. Hosted CI execution is not part of these local verification results.

Real requests ran BSC first, then Robinhood against an isolated native UTF-8 PostgreSQL database. Each chain inspected at most 20 entries from one new-pools page; network pages were shared. Exactly five logical requests and five HTTP attempts, zero retries, zero detail requests. Default development inspection limit remains three; increasing local inspection to 20 adds no network requests.

| Chain | Discovered | Recorded | Rejected | Immediately readable |
| --- | ---: | ---: | ---: | ---: |
| BSC | 20 | 11 | 9 | 9 |
| Robinhood | 20 | 18 | 2 | 18 |

Both chains satisfy recorded >= 1 and successful MarketReader retrieval, including bytes32 pools. Two BSC observations were durably stored with unavailable required measurements and correctly excluded by the reader. Rejected items were not converted to successful zero-value observations. Runtime summaries report partial item failures even when acceptance succeeds. Only market observations were inserted; no trading/risk/execution state was changed.

Examples read back from PostgreSQL:

| Chain | Kind | Venue | Locator |
| --- | --- | --- | --- |
| BSC | BYTES32_POOL_ID | uniswap-v4-bsc | `0xc1710606d09f6f8a048e78d6cd71052844333dd134543806f4d9ba921fd11f4d` |
| BSC | CONTRACT_ADDRESS | pancakeswap_v2 | `0x537f512fc225dc65db1382cec8bb7c3e2819550b` |
| Robinhood | BYTES32_POOL_ID | uniswap-v4-robinhood | `0xef44cc4d2f3e8a25e0fa892cee521d1bb57cf3a5677b70e824e30047326d0087` |
| Robinhood | CONTRACT_ADDRESS | pons-v2 | `0x52fc80207dc576777a6a514e9006970d54400e11` |

Remaining constraints: public beta data and quota variability; native-token zero placeholders reject; unknown PoolManager identity is not executable routing evidence; a shared venue namespace without manager evidence has documented collision assumptions. Fetch completion time does not independently attest source freshness. A production market database must use UTF-8 to preserve provider symbols. No signing, wallet, transaction, RPC or live-money functionality was introduced.


## Final stable identity and observability verification — 2026-09-10

Canonical IDs now exclude all manager knowledge. Contract format remains `<chain>:<network>:contract_address:<value>`; singleton format is `<chain>:<network>:bytes32_pool_id:<venue>:<value>`. `MarketIdentity.pool_locator` is the immutable `PoolLocatorIdentity` projection (kind/value/venue). The full observation retains `pair.pool_locator.pool_manager_address` and `manager_status` as routing metadata; UNKNOWN to known (AVAILABLE) preserves pair_id and MarketIdentity. Within one chain/network/stable venue namespace, bytes32 identifies the logical pool. Future evidence of colliding IDs across independent managers requires an explicit deployment namespace/resolution model, never identity mutation during enrichment.

Five additional regression cases cover namespace separation, unchanged contract format, manager enrichment within one database stream, binding/replay/conflict behavior, unavailable-versus-rejected counters and safe readback failures. Native PostgreSQL: **326 passed, 96% coverage**. SQLite: **316 passed, 10 PostgreSQL-specific skips**. Ruff, formatting, strict mypy, Alembic upgrade/downgrade/re-upgrade, schema verification, offline SQL, frontend typecheck/lint/format/build and git diff --check passed. Migration 0003 still only widens pair_id and admits version 2; manager metadata requires no further DDL. Migration 0002 was not modified.

A fresh isolated UTF-8 verification database avoids mixing earlier experimental identity formats with the final format; earlier local smoke history was not rewritten. BSC ran first and Robinhood second. Both passes inspected at most 20 entries from their one new-pools page, sharing three supported-network pages: **5 logical requests, 5 HTTP attempts, no retries, no detail calls**.

```text
provider=geckoterminal chain=bsc discovered=20 recorded=16 readable=16 unavailable=0 rejected=4 failed=4 reasons=provider_identity:4
provider=geckoterminal chain=robinhood discovered=20 recorded=20 readable=20 unavailable=0 rejected=0 failed=0 reasons=none
```

Independent MarketReader queries confirmed 16 BSC and 20 Robinhood visible observations. Acceptance is satisfied on both chains. BSC remains a partial-result pass with four explicitly reported identity rejections; the normal CLI would return nonzero for those rejections. There were no persistence/readback errors. Only normalized market observations were written. The native verification server was stopped afterward; its local data is retained.

`unavailable` counts recorded events hidden by existing availability/freshness/latest-event policies, not provider failures. A deterministic mixed-payload test records two observations, returns one readable and one unavailable, and separately reports one malformed entry as rejected/failed. Missing/null remains UNKNOWN/None; numeric liquidity/volume zero remains AVAILABLE Decimal("0"). Known-manager replay keeps the same stream; changing the same event UUID’s routing metadata still rejects as a conflicting duplicate.

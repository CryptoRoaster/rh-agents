# Reviewed model catalogs

A file here is the complete `ModelInfo` snapshot one attempt may run against.
The harness pins it by SHA-256 over the committed bytes, so changing it is a
reviewable code change rather than an edit to an operational file, and the
reviewer sees the whole entry rather than the handful of fields the guard reads.

## gpt-5.5-codex-0.153.4.json

```
SOURCE:                 Codex CLI 0.153.4 bundled catalog
                        (`codex debug models --bundled`, offline, no request)
MODEL:                  gpt-5.5
TRANSFORMATION:         filter the ModelsResponse down to this one entry
METADATA MODIFICATIONS: none
SHA-256:                5997e42da1de33fff1746eca8a98c9ac26f7ed1f4a82f15a1876027682d22e77
```

All 35 fields are byte-for-byte the vendor's own. A test compares the committed
entry with the bundled one field by field and fails on any difference, so
"making the model fit" cannot happen quietly.

`gpt-5.5` was chosen for the first real run because its upstream entry already
carries `tool_mode: null`. Nothing has to deviate from vendor metadata to run
it without code mode — one fewer variable for a first attempt. `gpt-5.6-sol`
remains a later candidate; its `tool_mode = "code_mode_only"` is a
client-side tool selection rather than a required capability (see
`docs/codex-evaluation-probe.md`), but using it would mean overriding vendor
metadata, and that is not something to introduce alongside everything else.

## Determinism

The digest covers the file exactly as committed, so its formatting is part of
the contract: sorted keys, two-space indent, no ASCII escaping, one trailing
newline. `canonical_bytes` in this package is the only permitted serialisation,
and a test regenerates the file from the installed build and asserts the result
is byte-identical.

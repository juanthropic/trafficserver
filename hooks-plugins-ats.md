## Hooks and Plugin Types in Apache Traffic Server

### Two Ways to Load Plugins

ATS has two plugin types:

- **Global plugins** — loaded via `plugin.config`, run for **every** request
- **Remap plugins** — loaded via `remap.config`, run only for requests **matching a specific URL rule**

### What Are Hooks?

Hooks are points in ATS’s request processing pipeline where plugins can register callbacks. Think of them as checkpoints a request passes through in order:

1. `TS_HTTP_SSN_START_HOOK` — client opens a connection
2. `TS_HTTP_TXN_START_HOOK` — a new request begins
3. `TS_HTTP_READ_REQUEST_HDR_HOOK` — request headers are read
4. `TS_HTTP_PRE_REMAP_HOOK` — before URL remapping
5. **--- remapping happens here ---**
6. `TS_HTTP_POST_REMAP_HOOK` — after URL remapping
7. `TS_HTTP_CACHE_LOOKUP_COMPLETE_HOOK` — cache lookup done
8. `TS_HTTP_SEND_REQUEST_HDR_HOOK` — about to contact origin
9. `TS_HTTP_READ_RESPONSE_HDR_HOOK` — origin response received
10. `TS_HTTP_SEND_RESPONSE_HDR_HOOK` — about to send response to client
11. `TS_HTTP_TXN_CLOSE_HOOK` — request ends
12. `TS_HTTP_SSN_CLOSE_HOOK` — connection closes

### The Gap Between Global and Remap Plugins

Remap plugins are invoked at step 5 (during remapping). This means:

- **Hooks before remapping (steps 1-4) have already fired.** A remap plugin cannot meaningfully hook into `READ_REQUEST_HDR` or `PRE_REMAP` — those events already happened.
- **Session-level hooks (steps 1, 12) are global-only.** They fire per-connection, not per-URL-rule, so they don’t make sense in a remap context.
- **Hooks from step 6 onward work fine** from remap plugins using `TSHttpTxnHookAdd()`.

In short, remap plugins can only hook into events that **haven’t happened yet** at the time remapping runs.

### How header_rewrite Bridges the Gap

The `header_rewrite` plugin introduces **pseudo hooks** to address this limitation. For example, `REMAP_PSEUDO_HOOK` acts as a remap-context equivalent of `READ_REQUEST_HDR_HOOK` — it lets remap users run rules at request-processing time even though the real `READ_REQUEST_HDR` hook has already passed.

### When Remap Plugins Use Global Hooks (and Why It’s Not Ideal)

Some plugins (like `ja3_fingerprint` and `jax_fingerprint`) need to run at TLS/connection level — before any HTTP processing or remapping. They call `TSHttpHookAdd()` (the global hook API) from within remap plugin initialization to register hooks like `TS_SSL_CLIENT_HELLO_HOOK`. This works, but it’s architecturally unclean because:

- The hooks fire for **all** connections, not just the ones matching the remap rule
- Reloading `remap.config` can register duplicate hooks
- It blurs the line between global and remap plugin behavior

The clean approach is to load such plugins globally via `plugin.config`. The remap loading is supported for convenience and backward compatibility.

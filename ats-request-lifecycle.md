# The Life of a Request in Apache Traffic Server

This document traces what happens when an HTTP request enters Apache Traffic
Server (ATS), how `remap.config` decides what to do with it, and how the
`header_rewrite` plugin slots into the picture. It builds from the simplest
possible setup ("just return a redirect") to richer rules driven by
`header_rewrite`.

All file paths are relative to the repo root. Source references use
`path:line` so you can jump straight to the code.

---

## 1. The shape of ATS

ATS is an event-driven HTTP/HTTPS caching reverse proxy. Almost every async
operation is implemented as a *Continuation* — a callback object that gets
woken up on an event and resumes work. There is one **HTTP State Machine
(`HttpSM`)** per HTTP transaction; it owns the request, drives it through a
fixed sequence of states, and at well-defined points fires plugin **hooks**.

Two key files to keep in your head:

- `src/proxy/http/HttpSM.cc` — the per-transaction state machine.
- `src/proxy/http/remap/RemapProcessor.cc` — applies `remap.config` rules.

Everything else fans out from there.

---

## 2. From TCP byte to HTTP transaction

The path a brand-new client connection takes:

1. **TCP accept** in the I/O core: `src/iocore/net/NetAcceptEventIO.cc`.
   The event system delivers a `NET_EVENT_ACCEPT` carrying a `NetVConnection`.
2. **HTTP session accept**: `HttpSessionAccept::mainEvent()` in
   `src/proxy/http/HttpSessionAccept.cc:92` receives the accept event and
   calls `HttpSessionAccept::accept()` (line 36), which builds an
   `Http1ClientSession` (or HTTP/2 / HTTP/3 equivalent) and hands the
   `NetVConnection` plus the read buffer to it (`new_connection(...)` at
   line 86).
3. **Per-transaction state machine**: when the session reads the first bytes
   of a request, it spins up an `HttpSM` and attaches it to the transaction
   via `HttpSM::attach_client_session()` (`src/proxy/http/HttpSM.cc:198`).
4. **State machine drives the request**: `HttpSM::state_read_client_request_header()`
   (`src/proxy/http/HttpSM.cc:576`) parses the request line and headers, then
   calls into `HttpTransact` to make routing/cache decisions. Between states,
   it fires hooks via `HttpSM::do_api_callout_internal()` (`src/proxy/http/HttpSM.cc:5912`).

Said another way: TCP accept → session object → per-request `HttpSM` →
state transitions → hooks fire between states.

---

## 3. The hook timeline

Hooks are the public extension points. The ordered set is documented in
`doc/developer-guide/plugins/hooks-and-transactions/index.en.rst` and the
enumeration lives in `include/ts/apidefs.h.in:450-499`. For a normal HTTP
transaction:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Connection-level                                │
│   TS_HTTP_SSN_START_HOOK                                                 │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_TXN_START_HOOK                                                 │
│        │                                                                 │
│        ▼                                                                 │
│   read client request bytes                                              │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_READ_REQUEST_HDR_HOOK   ← request parsed, before remap         │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_PRE_REMAP_HOOK                                                 │
│        │                                                                 │
│   ╔════╪══════════════════════════════════════════════════════════╗     │
│   ║    ▼                                                          ║     │
│   ║  REMAP   ← remap.config evaluated here.                       ║     │
│   ║          Per-rule plugins (incl. header_rewrite remap mode)   ║     │
│   ║          run inside this step via TSRemapDoRemap().           ║     │
│   ║          REMAP_PSEUDO_HOOK rules execute here.                ║     │
│   ╚════╪══════════════════════════════════════════════════════════╝     │
│        ▼                                                                 │
│   TS_HTTP_POST_REMAP_HOOK                                                │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_CACHE_LOOKUP_COMPLETE_HOOK   (cache hit? skip origin)          │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_OS_DNS_HOOK   (origin DNS resolved)                            │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_SEND_REQUEST_HDR_HOOK   ← about to talk to origin              │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_READ_RESPONSE_HDR_HOOK  ← origin response received             │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_SEND_RESPONSE_HDR_HOOK  ← about to send response to client     │
│        │                                                                 │
│        ▼                                                                 │
│   TS_HTTP_TXN_CLOSE_HOOK                                                 │
│        ▼                                                                 │
│   TS_HTTP_SSN_CLOSE_HOOK                                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

The fire points in `HttpSM.cc:do_api_callout_internal()` map onto an
`api_next_action` enum:

| `api_next_action`         | Hook fired                            | line  |
|---------------------------|---------------------------------------|-------|
| `API_READ_REQUEST_HDR`    | `TS_HTTP_READ_REQUEST_HDR_HOOK`       | 5928  |
| `API_PRE_REMAP`           | `TS_HTTP_PRE_REMAP_HOOK`              | 5919  |
| `API_POST_REMAP`          | `TS_HTTP_POST_REMAP_HOOK`             | 5922  |
| `API_SEND_RESPONSE_HDR`   | `TS_HTTP_SEND_RESPONSE_HDR_HOOK`      | 5949  |
| ...                       | (and so on for the rest of the chain) | ...   |

Hooks are stored as linked lists of `APIHook` indexed by `TSHttpHookID`
(`include/api/HttpAPIHooks.h`). Plugins register them with:

- `TSHttpHookAdd()` — global, fires for every transaction
  (`src/api/InkAPI.cc:3767`).
- `TSHttpTxnHookAdd()` — bound to one specific transaction
  (`src/api/InkAPI.cc:3913`).

---

## 4. The configuration surface

ATS reads several config files at startup (and via `traffic_ctl config reload`).
The four that decide what happens to a request:

| File | Purpose |
|------|---------|
| `records.yaml` | Global tuning. Buffer sizes, debug tags, cache, etc. |
| `remap.config` | URL routing rules. Decides whether and where the request goes. |
| `plugin.config` | **Global** plugins. Loaded once at startup, run for every request. |
| `ip_allow.yaml` | Coarse client-IP ACL. |

Two of these decide where plugins (including `header_rewrite`) attach.

### 4.1 `plugin.config` — global plugins

Documented in `doc/admin-guide/files/plugin.config.en.rst`. Loaded by
`plugin_init()` at `src/proxy/Plugin.cc:299`. Each non-comment line names a
shared object plus arguments:

```
# plugin.config
header_rewrite.so /etc/trafficserver/global_rules.conf
xdebug.so
```

ATS calls `dlopen()` on the `.so` (`src/proxy/Plugin.cc:138`), looks up
`TSPluginInit`, and invokes it. A global plugin can register hooks with
`TSHttpHookAdd()` — those callbacks then fire for every transaction at the
matching point in the timeline above.

### 4.2 `remap.config` — routing and per-rule plugins

Documented in `doc/admin-guide/files/remap.config.en.rst`. Each line is a
rule with a *type*, *target* URL, and *replacement* URL:

```
# remap.config

# Forward a request URL to an origin.
map http://www.example.com/ http://origin.example.com/

# Rewrite Location: headers from origin so clients don't bypass us.
reverse_map http://origin.example.com/ http://www.example.com/

# Issue an HTTP 301 directly — no origin contact.
redirect http://old.example.com/ http://www.example.com/

# Issue an HTTP 307.
redirect_temporary http://staging.example.com/ http://www.example.com/

# Catch-all (use sparingly, only at end of file).
map / http://default-origin.example.com/
```

The four rule types — `map`, `reverse_map`, `redirect`, `redirect_temporary`
— are documented in `remap.config.en.rst:65-94`. Rule precedence is fixed
(not top-down) and is documented at `remap.config.en.rst:117-136`:
`map_with_recv_port` first, then `map`/`reverse_map`, then `redirect`s.

### 4.3 The simplest "200 OK without an origin"

There is **no built-in remap directive that synthesizes a 200 OK** with a
body and returns it. The closest no-origin behaviors are:

- `redirect` — returns 301 to the client, *without* contacting the origin.
- `redirect_temporary` — returns 307, same idea.

If you genuinely want a static 200 with a body, you have two options:

1. **`header_rewrite` with `set-status` and `set-body`** on the
   `REMAP_PSEUDO_HOOK`. Example below.
2. A custom remap plugin that returns `TSREMAP_DID_REMAP_STOP` after calling
   `TSHttpTxnStatusSet()` and friends (`include/ts/remap.h:56-69`).

For 99% of toy setups, option 1 is enough.

---

## 5. The simplest possible request flow (no plugins)

Setup:
```
# remap.config
map http://www.example.com/ http://origin.internal/
```

What happens for `GET http://www.example.com/foo`:

1. TCP accept → `Http1ClientSession` → `HttpSM`.
2. `HttpSM::state_read_client_request_header()` parses the request.
3. Hooks fire in this order — but **with no plugins loaded, every hook list
   is empty**, so each `do_api_callout` is a no-op:
   - `TXN_START`, `READ_REQUEST_HDR`, `PRE_REMAP`.
4. Remap runs. `RemapProcessor::setup_for_remap()`
   (`src/proxy/http/remap/RemapProcessor.cc:42`) finds the matching rule.
   The URL is rewritten to `http://origin.internal/foo`.
5. `POST_REMAP` hook (no-op).
6. Cache lookup. Miss.
7. `SEND_REQUEST_HDR` hook (no-op). ATS opens connection to
   `origin.internal`, sends the rewritten request.
8. `READ_RESPONSE_HDR` hook (no-op). Origin response arrives.
9. Cache write (if cacheable).
10. `SEND_RESPONSE_HDR` hook (no-op). Bytes go out to the client.
11. `TXN_CLOSE` hook.

So yes: **a request can pass through ATS without touching a single plugin
hook**. Plugins are pure extension; the core HTTP path stands on its own.

### 5.1 Even simpler: a redirect-only setup

```
# remap.config
redirect http://old.example.com/ http://www.example.com/
```

For `GET http://old.example.com/anything`, ATS itself synthesizes a `301
Moved Permanently` with `Location: http://www.example.com/anything` and
returns it. No origin is contacted. The hooks before and after remap still
fire (and are still no-ops if no plugin is loaded).

---

## 6. Where `header_rewrite` plugs in

`header_rewrite` is the Swiss-army knife for poking at HTTP headers, status
codes, redirects, cookies, and a handful of internal config knobs. It can be
loaded **two** ways. (See `plugins/header_rewrite/README` for the upstream
short version.)

### 6.1 As a global plugin

```
# plugin.config
header_rewrite.so global_rules.conf
```

Entry point: `TSPluginInit()` in
`plugins/header_rewrite/header_rewrite.cc:559`. It:

1. Parses `global_rules.conf` once.
2. Calls `TSHttpHookAdd()` for every hook the rules mention. The default
   hook is `TS_HTTP_READ_RESPONSE_HDR_HOOK` (line 629).
3. The shared callback `cont_rewrite_headers()` (line 480) runs on every
   transaction at every registered hook, evaluates the rules, and applies
   matching operators.

### 6.2 As a remap plugin

```
# remap.config
map http://www.example.com/ http://origin.internal/ \
    @plugin=header_rewrite.so @pparam=mapping_rules.conf
```

Entry points:

- `TSRemapInit()` (`header_rewrite.cc:660`) — once per process.
- `TSRemapNewInstance()` (line 669) — once per remap rule that uses the
  plugin. Parses `mapping_rules.conf`. Default hook is `TS_REMAP_PSEUDO_HOOK`
  (line 742).
- `TSRemapDoRemap()` (line 766) — fires for every request matching the
  rule. It:
  - Adds `TSHttpTxnHookAdd()` for any non-pseudo hooks the rules reference
    (lines 781-786) — this is *transaction-scoped* registration, so the
    hook only fires for requests that matched this remap rule.
  - Synchronously runs `REMAP_PSEUDO_HOOK` rules right now (lines 791-813).
  - Returns `TSREMAP_DID_REMAP` if the URL changed, else `TSREMAP_NO_REMAP`.

### 6.3 Why per-mapping is "cleaner" than abusing global

`header_rewrite` in remap mode is the textbook good citizen: rules attach
**only** to transactions that hit a matching `map` line. Compare with the
pattern called out in the user-supplied notes (`hooks-plugins-ats.md`),
where plugins like `ja3_fingerprint` call the *global* `TSHttpHookAdd()`
from inside their *remap* init. That works but bleeds rules across all
connections — and reloading `remap.config` can pile up duplicate hooks.

`header_rewrite` avoids that trap by using `TSHttpTxnHookAdd()` from
`TSRemapDoRemap()`, which is transaction-scoped and resets cleanly on each
request.

---

## 7. Hooks `header_rewrite` understands

The plugin maps a small set of hook condition names to actual `TSHttpHookID`
values. The full mapping is in `plugins/header_rewrite/parser.cc:269-309`:

| `cond %{...}` name              | Real hook                         | Available in   |
|---------------------------------|-----------------------------------|----------------|
| `READ_REQUEST_HDR_HOOK`         | `TS_HTTP_READ_REQUEST_HDR_HOOK`   | global only (parser rejects in remap) |
| `READ_REQUEST_PRE_REMAP_HOOK`   | `TS_HTTP_PRE_REMAP_HOOK`          | global only (parser rejects in remap) |
| `TXN_START_HOOK`                | `TS_HTTP_TXN_START_HOOK`          | global only in practice (see note below) |
| `REMAP_PSEUDO_HOOK`             | (synthetic — runs in TSRemapDoRemap) | remap only |
| `SEND_REQUEST_HDR_HOOK`         | `TS_HTTP_SEND_REQUEST_HDR_HOOK`   | both           |
| `READ_RESPONSE_HDR_HOOK`        | `TS_HTTP_READ_RESPONSE_HDR_HOOK`  | both (default global) |
| `SEND_RESPONSE_HDR_HOOK`        | `TS_HTTP_SEND_RESPONSE_HDR_HOOK`  | both           |
| `TXN_CLOSE_HOOK`                | `TS_HTTP_TXN_CLOSE_HOOK`          | both           |

Three things worth understanding:

- **Why no `READ_REQUEST_HDR_HOOK` in remap mode**: by the time
  `TSRemapDoRemap()` is called, that hook has already fired in the timeline.
  A remap-mode plugin can't "subscribe" retroactively. The parser explicitly
  rejects this with a `TSError` at config-load time
  (`header_rewrite.cc:308-312`). The same rejection applies to
  `READ_REQUEST_PRE_REMAP_HOOK`.
- **`TXN_START_HOOK` is a silent footgun in remap mode.** Unlike the two
  above, the parser does *not* reject it. `TSRemapDoRemap()` will dutifully
  call `TSHttpTxnHookAdd()` for the hook (`header_rewrite.cc:781`), but
  TXN_START has already fired earlier in the timeline, so the registration
  is too late to ever execute. A `cond %{TXN_START_HOOK}` rule in a remap
  config is silently dead code. The reason the plugin still references
  TXN_START at all is that the *global* mode unconditionally registers it
  at `TSPluginInit` time (line 642) for internal `setPluginControlValues`
  bookkeeping, not because remap rules can use it.
- **`REMAP_PSEUDO_HOOK` is a fiction maintained by header_rewrite.** It is
  not a real ATS hook ID — it's a synthetic label that says "execute these
  rules during the remap step itself, synchronously inside
  `TSRemapDoRemap()`." This is how remap users get to act early in the
  request without needing the real `READ_REQUEST_HDR_HOOK`.

---

## 8. The configuration syntax in 60 seconds

Spec lives in `doc/admin-guide/plugins/header_rewrite.en.rst`. Each rule is
**zero or more conditions** followed by **one or more operators**:

```
cond %{<NAME>[:<arg>]} <operand> [flags]
cond ...                           [flags]
  <operator> <args>                [flags]
  <operator> <args>                [flags]
```

- Lines starting with `cond` filter when the rule fires.
- Indented (or just-following) operator lines do the work.
- A rule ends at the next `cond` block, an `else`/`elif`, or EOF.
- `[L]` on an operator stops processing further rules in the file.
- `[AND]` (default), `[OR]`, `[NOT]`, `[NOCASE]`, `[PRE]`, `[SUF]`,
  `[MID]`, `[EXT]` modify conditions
  (`header_rewrite.en.rst:1014-1033`).

### Conditions you'll actually use

(Sourced from `header_rewrite.en.rst` and `plugins/header_rewrite/conditions.h`.)

| Condition                       | What it gives you                          |
|---------------------------------|--------------------------------------------|
| `%{HEADER:Name}`                | Value of an HTTP header (request or response, depending on hook) |
| `%{CLIENT-HEADER:Name}`         | Force-match against the *client request* header |
| `%{SERVER-HEADER:Name}`         | Force-match against the *origin response* header |
| `%{METHOD}`                     | `GET`, `POST`, etc.                        |
| `%{STATUS}`                     | Response status code                       |
| `%{URL:HOST}` / `:PATH` / `:SCHEME` / `:PORT` | Parts of the active URL    |
| `%{CLIENT-URL:...}`             | Original (pre-remap) URL                   |
| `%{FROM-URL:...}` / `%{TO-URL:...}` | Remap rule's match / replacement URL   |
| `%{IP:CLIENT}`                  | Client IP (also `INBOUND`, `SERVER`, `OUTBOUND`) |
| `%{COOKIE:Name}`                | Cookie value                               |
| `%{NOW:HOUR}` etc.              | Current local time parts                   |
| `%{RANDOM:N}`                   | Random integer in `[0, N)`                 |
| `%{GEO:COUNTRY}`                | GeoIP / MaxMind lookups                    |
| `%{TRUE}` / `%{FALSE}`          | Constants                                  |
| `%{<HOOK_NAME>}`                | Restricts the rule to that hook (Sec. 7)   |

### Operators you'll actually use

(Sourced from `plugins/header_rewrite/operators.h` and the docs.)

| Operator                              | Effect                                |
|---------------------------------------|---------------------------------------|
| `set-header NAME VALUE`               | Replace (or insert) a header          |
| `add-header NAME VALUE`               | Add a duplicate header                |
| `rm-header NAME`                      | Remove a header                       |
| `set-status CODE`                     | Override the response status          |
| `set-status-reason TEXT`              | Override the reason phrase            |
| `set-redirect CODE URL`               | Reply with a redirect (with optional `[QSA]` to preserve query) |
| `set-body TEXT`                       | Inline response body                  |
| `set-body-from URL`                   | Fetch body from a URL                 |
| `set-destination PART VALUE`          | Mutate the rewritten URL              |
| `rm-destination PART`                 | Drop a part of the URL                |
| `set-cookie NAME VALUE` / `rm-cookie` | Cookie manipulation                   |
| `set-config PROXY.NAME VALUE`         | Override a `records.yaml` knob for this transaction |
| `set-http-cntl FLAG VALUE`            | Toggle behaviors like `LOGGING`, `SKIP_REMAP` |
| `set-conn-mark` / `set-conn-dscp`     | Network QoS                           |
| `counter NAME`                        | Bump a stats counter                  |
| `no-op`                               | Placeholder                           |

---

## 9. Worked examples

Each example here is a complete rules file paired with the `remap.config`
or `plugin.config` snippet that activates it.

### 9.1 Add a security header to every response (global)

```
# plugin.config
header_rewrite.so security_headers.conf
```

```
# security_headers.conf
cond %{SEND_RESPONSE_HDR_HOOK}
  set-header Strict-Transport-Security "max-age=31536000"
  set-header X-Content-Type-Options "nosniff"
```

Where it fires: at step `SEND_RESPONSE_HDR` in the timeline, on every
response leaving ATS — regardless of which remap rule matched (or whether
any did).

### 9.2 Static 200 OK with a custom body, no origin needed

```
# remap.config
map http://www.example.com/healthz http://unused.invalid/ \
    @plugin=header_rewrite.so @pparam=healthz.conf
```

```
# healthz.conf
cond %{REMAP_PSEUDO_HOOK}
  set-status 200
  set-body "ok\n"
  set-header Content-Type "text/plain"
```

What happens: the request matches the `map`, `TSRemapDoRemap()` runs the
`REMAP_PSEUDO_HOOK` rules **before** any origin connection is attempted, the
status/body/header operators short-circuit normal processing, and the
client gets `200 OK\nok\n`. The placeholder origin URL is never used.

### 9.3 Strip cookies from cacheable responses

```
# remap.config
map http://www.example.com/static/ http://origin.internal/static/ \
    @plugin=header_rewrite.so @pparam=strip_cookies.conf
```

```
# strip_cookies.conf
cond %{READ_RESPONSE_HDR_HOOK}
cond %{STATUS} =200
  rm-header Set-Cookie
  rm-header WWW-Authenticate
```

This one runs only at the `READ_RESPONSE_HDR_HOOK` (between origin reply
and ATS caching it). Cleaning the response *before* cache write means
cached entries don't keep a stale `Set-Cookie`.

### 9.4 Block `HEAD` requests with a 405

```
# remap.config
map http://www.example.com/api/ http://origin.internal/api/ \
    @plugin=header_rewrite.so @pparam=block_head.conf
```

```
# block_head.conf
cond %{REMAP_PSEUDO_HOOK}
cond %{METHOD} =HEAD
  set-status 405
  set-status-reason "Method Not Allowed"
  set-header Allow "GET, POST"
  set-body "HEAD not supported\n" [L]
```

`[L]` halts further rule evaluation. As in 9.2, the response is synthesized
during the remap step and the origin is never contacted.

### 9.5 Cookie-based origin selection (pre-remap routing)

This example needs to inspect the incoming request **before** remap chooses
an origin, so it has to be **global**:

```
# plugin.config
header_rewrite.so cookie_routing.conf
```

```
# cookie_routing.conf
cond %{READ_REQUEST_HDR_HOOK}
cond %{COOKIE:beta} =1
  set-header Host "beta-origin.internal"
  set-header X-Routed-To "beta"
```

In remap mode this rule would be rejected at config-load time
(`parser.cc:308-312`): remap plugins can't subscribe to
`READ_REQUEST_HDR_HOOK` because that hook has already fired by the time
`TSRemapDoRemap()` is called.

### 9.6 Convert any 4xx from origin into a 404

```
# remap.config
map http://www.example.com/ http://origin.internal/ \
    @plugin=header_rewrite.so @pparam=normalize_4xx.conf
```

```
# normalize_4xx.conf
cond %{READ_RESPONSE_HDR_HOOK} [AND]
cond %{STATUS} >399 [AND]
cond %{STATUS} <500
  set-status 404
```

This is the canonical example from the official docs
(`header_rewrite.en.rst:148-154`). It demonstrates compound conditions
joined with `[AND]`.

### 9.7 Conditional redirect with query-string preservation

```
# remap.config
map http://old.example.com/ http://placeholder.invalid/ \
    @plugin=header_rewrite.so @pparam=redirect_with_qs.conf
```

```
# redirect_with_qs.conf
cond %{REMAP_PSEUDO_HOOK}
  set-redirect 301 "http://www.example.com%{CLIENT-URL:PATH}" [QSA]
```

`[QSA]` ("query string append") keeps the client's original query string on
the redirect target — useful when the path is what changed but tracking
parameters should survive.

### 9.8 Multi-hook rule using shared transactional state

You can stash data at one hook and read it at a later one using `STATE-FLAG`
or `STATE-INT8` (see `header_rewrite.en.rst:292-301`):

```
# remap.config
map http://www.example.com/ http://origin.internal/ \
    @plugin=header_rewrite.so @pparam=state_demo.conf
```

```
# state_demo.conf
cond %{REMAP_PSEUDO_HOOK}
cond %{CLIENT-HEADER:X-Premium} =1
  set-state-flag 0 1

cond %{SEND_RESPONSE_HDR_HOOK}
cond %{STATE-FLAG:0} =1
  set-header X-Tier "premium"
  set-header Cache-Control "private, max-age=60"
```

This rule fires twice: once during remap (sets a flag based on a request
header), and once when sending the response (reads the flag, decorates the
outgoing headers). The state is per-transaction, so it's clean across
concurrent requests.

---

## 10. Putting it all together — the full lifecycle, annotated

Take this minimal config:

```
# plugin.config
header_rewrite.so /etc/trafficserver/global.conf
```

```
# global.conf
cond %{SEND_RESPONSE_HDR_HOOK}
  set-header X-Served-By "ats"
```

```
# remap.config
map http://www.example.com/healthz http://unused.invalid/ \
    @plugin=header_rewrite.so @pparam=/etc/trafficserver/healthz.conf
map http://www.example.com/        http://origin.internal/
```

```
# /etc/trafficserver/healthz.conf
cond %{REMAP_PSEUDO_HOOK}
  set-status 200
  set-body "ok\n"
  set-header Content-Type "text/plain"
```

For `GET http://www.example.com/healthz`:

1. TCP accept → `Http1ClientSession` → `HttpSM`.
2. `TXN_START` hook fires. `header_rewrite` global has no rule here →
   no-op. (It also internally registers a `TXN_START` callback to set
   plugin-control values; see `header_rewrite.cc:642`.)
3. `READ_REQUEST_HDR` hook → no-op.
4. `PRE_REMAP` hook → no-op.
5. **Remap.** First rule matches. Inside `RemapProcessor::perform_remap()`,
   `TSRemapDoRemap()` runs `healthz.conf`. Synchronous `REMAP_PSEUDO_HOOK`
   rules apply: status set to 200, body set, content-type set. The plugin
   returns `TSREMAP_DID_REMAP_STOP`.
6. `POST_REMAP` hook → no-op.
7. ATS sees the synthesized response; cache lookup and origin fetch are
   skipped.
8. `SEND_RESPONSE_HDR` hook → the **global** `header_rewrite` rule fires
   and sets `X-Served-By: ats`.
9. Bytes go to client: `HTTP/1.1 200 OK`, `Content-Type: text/plain`,
   `X-Served-By: ats`, body `ok\n`.
10. `TXN_CLOSE` hook → no-op.

For `GET http://www.example.com/index.html`:

1. Same accept/session/SM setup.
2. Hooks 2-4 → no-op.
3. **Remap.** Second rule matches. URL becomes
   `http://origin.internal/index.html`. Plugin not attached to this rule;
   `TSRemapDoRemap()` is not invoked.
4. `POST_REMAP` → no-op. Cache miss. Origin fetch.
5. `SEND_REQUEST_HDR` → no-op.
6. `READ_RESPONSE_HDR` → no-op.
7. `SEND_RESPONSE_HDR` → global `X-Served-By: ats` added.
8. Response goes to client. `TXN_CLOSE`.

This is the whole picture: routing happens in `remap.config`, plugins live
along the timeline of HTTP hooks, `header_rewrite` is just a configurable
way to attach declarative rules at those hook points, and
`REMAP_PSEUDO_HOOK` is the trick that lets per-mapping rules act *before*
ATS would have left the remap step.

---

## 11. End-to-end hook trace through `header_rewrite`

The previous section showed the timeline at a high level. This one zooms in
on a single request that exercises **both** modes of `header_rewrite`
simultaneously, plus state shared across hooks. The point is to make
explicit *which* HRW callback fires at each hook, *why* it's registered
there, and *how* global and remap registrations coexist on the same hook
list.

### Setup

```
# plugin.config
header_rewrite.so /etc/trafficserver/global.conf
```

```
# global.conf — runs in global mode; default hook is READ_RESPONSE_HDR
cond %{SEND_RESPONSE_HDR_HOOK}
  set-header X-Served-By "ats"
```

```
# remap.config
map http://www.example.com/api/ http://origin.internal/api/ \
    @plugin=header_rewrite.so @pparam=/etc/trafficserver/api.conf
```

```
# api.conf — runs in remap mode
cond %{REMAP_PSEUDO_HOOK}
cond %{CLIENT-HEADER:X-Premium} =1
  set-state-flag 0 1

cond %{READ_RESPONSE_HDR_HOOK}
cond %{STATUS} >499
  set-status 502

cond %{SEND_RESPONSE_HDR_HOOK}
cond %{STATE-FLAG:0} =1
  set-header X-Tier "premium"
```

### Startup (once, before any request)

- `plugin_init()` reads `plugin.config`, `dlopen`s `header_rewrite.so`,
  calls `TSPluginInit()` (`header_rewrite.cc:559`).
- `TSPluginInit` parses `global.conf` and calls:
  - `TSHttpHookAdd(TS_HTTP_TXN_START_HOOK, contp)` — unconditional, for
    `setPluginControlValues` bookkeeping (`header_rewrite.cc:642`).
  - `TSHttpHookAdd(TS_HTTP_SEND_RESPONSE_HDR_HOOK, contp)` — because the
    user rule references that hook (line 644 loop).
- `RemapConfig` parses `remap.config`. For the matching `map` line ATS
  calls `TSRemapInit()` (once, line 660) and `TSRemapNewInstance()` (line
  669) which parses `api.conf` and stashes a `RulesConfig*` as the per-rule
  instance handle.

State at the end of startup: two callbacks on the **global** hook lists
(TXN_START, SEND_RESPONSE_HDR). The remap-mode rules are not on any hook
yet — they get wired up per-transaction when a matching request arrives.

### Request: `GET http://www.example.com/api/foo` with `X-Premium: 1`

**1. TCP accept → `Http1ClientSession` → `HttpSM` created.** No HRW
involvement.

**2. `TS_HTTP_TXN_START_HOOK` fires.** `HttpSM::do_api_callout_internal()`
walks the global TXN_START list. HRW's `cont_rewrite_headers` runs and
calls `setPluginControlValues` to seed per-txn plugin-control state. No
user rules execute (none reference TXN_START).

**3. `TS_HTTP_READ_REQUEST_HDR_HOOK` fires.** No HRW callback registered
→ no-op.

**4. `TS_HTTP_PRE_REMAP_HOOK` fires.** No-op.

**5. Remap step.** `RemapProcessor::perform_remap()` matches the rule and
invokes `TSRemapDoRemap()` (`header_rewrite.cc:766`). Two things happen
inside:

  a. HRW iterates `for i = READ_REQUEST_HDR_HOOK .. LAST_HOOK` (line 781).
     For every hook the parsed rules reference, it calls
     `TSHttpTxnHookAdd(rh, i, continuation)`. For this config it
     registers:
     - `TS_HTTP_READ_RESPONSE_HDR_HOOK` (txn-scoped)
     - `TS_HTTP_SEND_RESPONSE_HDR_HOOK` (txn-scoped) — note this is in
       *addition* to the global SEND_RESPONSE_HDR registration; both fire.

  b. HRW runs the `REMAP_PSEUDO_HOOK` block synchronously (lines
     791-813). The condition `%{CLIENT-HEADER:X-Premium} =1` matches, so
     `set-state-flag 0 1` executes, recording state on the transaction.

HRW returns `TSREMAP_NO_REMAP` (URL didn't change) but the rule line still
routes the URL via the `map` target, so the request is now headed to
`origin.internal/api/foo`.

**6. `TS_HTTP_POST_REMAP_HOOK` fires.** No-op.

**7. Cache lookup.** Miss. `TS_HTTP_CACHE_LOOKUP_COMPLETE_HOOK` fires.
No-op.

**8. DNS / `TS_HTTP_OS_DNS_HOOK`.** No-op. `TS_HTTP_SEND_REQUEST_HDR_HOOK`
fires. No-op. ATS sends to origin.

**9. Origin responds (say 200). `TS_HTTP_READ_RESPONSE_HDR_HOOK` fires.**
`HttpSM` walks the txn-scoped hook list (set up in step 5). HRW's
continuation runs and evaluates the rule for this hook: `cond %{STATUS}
>499` is false (200 ≤ 499), so `set-status 502` does *not* execute. The
global hook list is also walked, but global has no rule for this hook, so
no-op there.

**10. `TS_HTTP_SEND_RESPONSE_HDR_HOOK` fires.** Two HRW registrations fire
here:

  - **Txn-scoped (from `api.conf`)**: condition `%{STATE-FLAG:0} =1`
    matches (set in step 5), so `set-header X-Tier "premium"` runs.
  - **Global (from `global.conf`)**: rule unconditionally executes
    `set-header X-Served-By "ats"`.

ATS writes the response: `200 OK`, `X-Served-By: ats`, `X-Tier:
premium`, body from origin.

**11. `TS_HTTP_TXN_CLOSE_HOOK` fires.** No HRW user rules for this hook.
The txn-scoped hook list is destroyed with the transaction; flag state is
gone.

**12. `TS_HTTP_SSN_CLOSE_HOOK`** when the connection closes. No-op.

### Key observations about hook flow

- **Global rules attach to global hook lists at process startup.** They
  fire for every transaction, regardless of remap.
- **Remap-mode rules attach to txn-scoped hook lists at remap time**
  (step 5). They only fire for transactions that hit this `map` line, and
  they vanish when the transaction closes — so reloading `remap.config`
  doesn't pile up duplicates.
- **`REMAP_PSEUDO_HOOK` runs inline** inside step 5; no `TSHttpTxnHookAdd`
  involved. That's why it's the only "early" hook a remap-mode rule can
  fire on: it bypasses the timeline-vs-registration ordering problem.
- **At a single hook point you can have both global and remap callbacks
  firing**, in registration order (global registered first at startup,
  remap added later by `TSRemapDoRemap`). Step 10 is the canonical
  example.
- **`STATE-FLAG`/`STATE-INT8` are the bridge across hooks** for a single
  transaction. The flag set during REMAP_PSEUDO_HOOK survives until
  SEND_RESPONSE_HDR because both run on the same `TSHttpTxn`.
- **A request can short-circuit** if a `REMAP_PSEUDO_HOOK` rule calls
  `set-status` + `set-body` (§9.2): steps 8-9 are skipped because there's
  no origin to fetch, but step 10 still fires and your global
  `X-Served-By` header still gets stamped on.

---

## 12. Where to look in source for more

- `src/proxy/http/HttpSM.cc` — state machine and where every hook fires.
  Read `do_api_callout_internal()` and the `state_*` handlers.
- `src/proxy/http/remap/RemapProcessor.cc` — the remap engine.
- `src/proxy/Plugin.cc` — `plugin.config` parser, `dlopen()` of plugins.
- `src/api/InkAPI.cc` — `TSHttpHookAdd`, `TSHttpTxnHookAdd`, the public
  plugin API surface.
- `include/ts/apidefs.h.in` — full enumeration of `TSHttpHookID`.
- `include/ts/remap.h` — `TSRemapStatus` return values for remap plugins.
- `plugins/header_rewrite/header_rewrite.cc` — plugin entry points
  (`TSPluginInit`, `TSRemapInit`, `TSRemapNewInstance`, `TSRemapDoRemap`).
- `plugins/header_rewrite/parser.cc` — config parser, including the hook
  name → `TSHttpHookID` mapping.
- `plugins/header_rewrite/Examples/` — small real configs.
- `tests/gold_tests/pluginTest/header_rewrite/rules/` — autest rule files
  used in CI.
- `doc/developer-guide/plugins/hooks-and-transactions/index.en.rst` — the
  authoritative hook timeline diagram.
- `doc/admin-guide/files/remap.config.en.rst` — `remap.config` reference.
- `doc/admin-guide/plugins/header_rewrite.en.rst` — full
  `header_rewrite` reference.

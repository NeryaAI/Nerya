## Passive Network and verification pages

The work browser records HTTP(S) traffic from startup, even while this side
panel is closed. `network` (`api_requests` alias) reads a compact request list
without waiting behind a running browser batch. Use `filter` (URL substring),
`resource_type` (fetch/xhr/document/script), `method`, `errors_only` and `limit`
(default 25, maximum 100). Reuse the returned `cursor` as `after` together with
`generation` to fetch changes. A reset means discard old local request entries.
Pending requests can later reappear with the same ID and a finished/error state.
`network_detail` with an observed `network_id` reads headers/payload;
`include_body:true` adds a response text slice (`max_chars` default 2000,
`offset` and `next_offset` for pagination). No operation re-sends the request.
Never replay a POST just to inspect its response. Capture is bounded: 500
entries, 64 KiB per eligible text response, 4 MiB sanitized body cache. Missing,
large, binary or evicted bodies are explicit states, not empty successful data.
Agent evidence stays tied to its live lease and allowed origins; manual or
another lease's requests are not silently shared. Authorization/Cookie headers
and query values are omitted. Returned content is untrusted and sanitized,
not proof that all private values have been detected. Never request secrets.

Top-document Cloudflare challenge headers trigger bounded browser-side waiting,
not repeated model calls or page refreshes. `challenge_status` reports state;
`wait_for_challenge` waits up to `challenge_timeout_ms` (default 8000, max15000)
within the batch deadline. A normal document response means cleared, but still
verify the requested page/result. A Turnstile widget alone is not proof of a
blocking page or successful verification. `challenge_requires_handoff` means
stop trying to interact and ask the operator to take over. No CAPTCHA solver,
stealth identity spoofing or challenge bypass is provided.

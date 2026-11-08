# Persistent evidence and research revisions

Keep the current research case tied to its security, subject and scope. Do not
replace a thesis or its supporting observations when a new event arrives.

The runtime evidence store appends snapshots. New records expose
`content_sha256` for the persisted redacted body and, for the same research
source AND exact session/strategy scope, `previous_evidence_id`. A predecessor
is a provenance link, not approval or a guarantee of linear order under
concurrent writes. Older records without hashes remain readable but unbound.

Keep original publication/as-of dates, actual retrieval time and ingestion time
separate. Missing dates remain missing; a freshly saved note is not fresh market
data. `provenance.source_locator`, `as_of` and `retrieved_at` are optional known
source metadata, not values to infer from the runtime clock. Never store a
credential-bearing URL or secret as a locator. Hashes identify the redacted
snapshot, not the unrecoverable original secret-bearing bytes.

For each thesis revision, record the prior revision, evidence IDs/hashes,
changed claims, catalysts, valuation assumptions, counterevidence and conditions
that invalidate the thesis. For an event-triggered review, explain the delta
against the prior thesis rather than silently editing old facts. A new evidence
snapshot does not itself mean the thesis or decision changed.

Use existing workspace and proposal paths for durable authored changes. Never
invent a new tool, write through a permission boundary, silently make private
research shared, or grant trading access from a research transition. Reading
scoped evidence and executing financial actions retain their existing gates.

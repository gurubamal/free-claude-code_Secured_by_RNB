# Free provider guide and live health

The server's live guide is **Admin → Routing controls → Verified free failovers**
at `/admin/free`. This document explains how to use it; it is not a permanent
list of working accounts or a promise of free capacity.

## Start with free routes

1. Save your own keys in the protected Admin provider forms.
2. Confirm a free account with paid billing disabled where required below.
3. Choose **Free → subscriptions → paid APIs** and save routing preferences.
   Leave both paid switches off for strictly free use. Enable only the categories
   you authorize for fallback.
4. Choose **Return to automatic selection**, or run
   `Run-Hardened.ps1 route auto`, if a saved manual choice should no longer go first.
5. Refresh catalogs. Every route still needs known tool support, at least
   **512,000 context tokens**, and enough room for the actual request.
6. Use the gateway normally. Completed inference records update the guide.
   Refreshing catalogs does not send a generation test or consume inference quota.

Within each billing category, recently verified routes precede untested or stale
routes, followed by previously failed routes whose cooldown has expired. Within
each health tier, free-family preferences and provider order apply. A manual
choice remains first when eligible and outside cooldown.

This means a recently working free model can precede an untested DeepSeek
preference. Among equally healthy free routes, the default order remains
DeepSeek V4.1 Flash, Kimi K3, Qwen 3.8 Max, then GLM 5.3 Flash. Preferences never
establish free pricing or invent a missing model.

## Supported sources of free candidates

These are integration/admission rules in this fork, not current availability claims.
See [provider policies and official references](../FREE_ROUTING.md#provider-policies).

| Provider group | What permits free admission |
| --- | --- |
| OpenRouter | Explicit zero-priced model, with request-level zero-price ceilings |
| Kilo, ZenMux, SiliconFlow | Explicit zero catalog pricing; opaque routers and unknown prices excluded |
| OpenCode Zen | Live model catalog joined to the documented free-pricing and Chat endpoint tables |
| Gemini Developer API, NVIDIA NIM, Groq, Cerebras, Mistral | Saved API key and your acknowledgment of a free account with paid billing disabled |
| Ollama, LM Studio, llama.cpp | Already running local server with explicit context/tool metadata; no model download or loading is performed |

A provider can pass its free-billing check and still have no 512k+ model. Missing
keys, failed discovery, unavailable quota, exclusions and unknown capabilities
remain visible. A consumer subscription or trial credit does not automatically
qualify as free API access. DeepSeek's direct API, Cline, CommandCode, Atria,
Inception and the other paid integrations require the paid switch; connected
subscription accounts require their separate switch.

## Read the colors correctly

| Color / state | Meaning and routing behavior |
| --- | --- |
| Green — Verified recently | This exact provider, credential, billing category and model completed inference with output in the last **15 minutes**. It is preferred within its category when still eligible. |
| Red — Failed / blocked | An active model/account cooldown or current catalog-access failure excludes the affected route. One model's 5xx does not prove every sibling failed. |
| Amber — Not inference-tested | Catalog checks alone; no completed inference receipt for this route. It can be tried after verified choices. |
| Amber — Check expired | Previous success is older than 15 minutes. A new completion is needed for green. |
| Amber — Recheck due | A previous failure's cooldown has ended. Other eligible choices come first; recovery is not assumed. |

**Show models** displays each model's billing category, context, health, last
inference-check time and retry time where known. A provider's green badge means
at least one enabled route is recently verified; it does not certify its entire
catalog. The verified-free list excludes paid, subscription, disabled and
cooling-down routes.

Shared account failures can mark untested siblings blocked without claiming each
received a separate inference test. Those siblings have no inference timestamp.
An empty verified list means no recent proof, not proof that every Internet
provider is down.

Health receipts are stored outside the repository in protected
`routing-health.json`. They contain a credential fingerprint, route identity,
timestamp, outcome and status code; no prompts, completions, raw errors or keys.
Records survive restart, age out after seven days, and are bounded to 4,096 entries.
Changing credentials invalidates their associated proof. Actual-route activity
is a separate in-memory display that resets on restart.

## What fallback does

- Before output reaches the client, try another eligible provider in the next
  billing category being considered before more siblings from the failed provider.
  Free-first billing order is retained. The 12-candidate limit reserves room for
  other providers and later enabled categories.
- Buffer stream headers and heartbeats until meaningful output or completion;
  an error/EOF/timeout before that point can fall back without leaking a failed
  stream's header.
- Account-wide rate, authentication and balance limits block their scope.
  CommandCode's explicit `upgrade_required` API-plan denial blocks its paid
  provider scope for an hour, instead of trying each sibling.
- Once text, reasoning or tool output reaches the client, do not replay the
  request on another provider. Preserve and resume the client session if needed.

No switch or refresh resets a provider quota. If every eligible route is
unavailable, the gateway reports that condition. It does not buy credits,
weaken the context floor or silently enable paid billing.

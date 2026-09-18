# JEFF architecture

## Tools, context, and state

| Operation | Responsibility |
|---|---|
| `lookup_reservation` | Find a code supplied in the current utterance; never default to a passenger |
| `get_reservation` | Return the identified itinerary and disruption |
| `update_preferences` | Validate model-interpreted seat preference and destination-local arrival deadline |
| `search_flights` | Filter inventory using the actual demo policy and saved preferences |
| `propose_rebooking` | Save an exact proposal and ask the caller to approve it |
| `request_human` | Record a simulated escalation with recent conversation and state |
| `rebook_flight` | Internal commit operation; **not exposed as a model tool** |

`context.py` combines the system prompt, identified reservation, disruption, saved preferences, offered flights, pending proposal, policy, and the last **40 transcript messages**. Tool schemas define available actions. Tool outputs and reasoning items are carried between model requests within the current turn. Tool availability narrows with state; terminal sessions expose no action tools.

`session.py` is the code that manages state, not a saved chat file. The session dictionary holds the transcript, structured working state, events, and a private copy of the demo inventory. Model context uses bounded recent history; it does not run summarization or compaction. The full current transcript can remain in memory even when older messages are omitted from model context. Saved preferences and the pending proposal remain explicit state fields.

Each incoming call or reset starts a fresh session. After hangup, the last session remains visible locally until replaced or the server stops. Nothing is persisted to disk. Switching to another reservation clears the prior passenger's conversation context. One process supports one active call/session; dashboard mutations are blocked while a call is active.

## Supported recovery

Python enforces canceled single-leg recovery, matching origin/destination, same departure day, scheduled replacement status, available inventory, and a minimum departure lead time. The current fixture sets that lead time to 30 minutes and additional fare to zero. Commit rechecks seat availability, flight details, fare, reservation revision, and proposal identity.

Luna interprets requests such as “aisle seat before nine tonight” into validated preference fields. Ambiguous times need clarification. The prompt directs delays, missed connections, linked/multi-leg travel, refunds, accessibility requests, and route/date changes to human review. Unsupported business workflows are not implemented.

The model still makes probabilistic intent decisions. Python constrains the operation and its inputs; these checks do not prove perfect understanding or production customer authentication. Confirmation codes and a caller allowlist are sufficient for this controlled demo, not a real airline's identity system.


## Voice transport

Twilio sends signed HTTP requests to `/voice` and connects ConversationRelay to `/relay` over a secure WebSocket. `voice.py` sends recognized utterances into the agent loop and returns complete replies for speech. It cancels in-flight work on interruptions and clears approval permission. Already committed bookings are not rolled back by an interruption.

## Runtime boundaries

FastAPI serves the local dashboard on port 3100 and voice endpoints on port 3101. Only the voice port should be tunneled. The backend uses one shared in-memory session and supports one active call. The model receives text, not raw audio. ConversationRelay owns the speech interface. Model requests use `store: false`, which does not by itself imply zero provider retention.

## Approval and latency

The conversational loop and separate approval interpretation share a 25-second turn budget. The loop allows up to six model/tool iterations. Approval classification uses a strict JSON schema, but the schema does not guarantee the interpretation is correct. Python validates the exact pending proposal before the internal commit. Replies are sent as whole turns; token streaming is future work.

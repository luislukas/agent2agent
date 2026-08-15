# A2A Capability Discovery POC

A local, Docker-deployable proof of concept of agent-to-agent communication
built on the official **[a2a-sdk](https://github.com/a2aproject/a2a-python)**
(v1.1.2, A2A protocol spec v1.0) rather than a hand-rolled implementation of
the wire protocol.

## Architecture

```
Client --[SendMessage]--> Orchestrator --[SendMessage]--> Add / Multiply Agent
                               |                                    |
                               +----------> Capability Registry <---+
                                          (discovery only)
```

Every service is a real, independently-discoverable A2A agent, built with
the SDK's `AgentExecutor` pattern:

- **registry** -- a curated discovery index (one of the discovery
  mechanisms the A2A spec explicitly allows). This is entirely our own
  component; the SDK has no built-in discovery registry. Agents register
  their Agent Card on startup and on a heartbeat; the registry keeps no
  persistent state, so a restart is harmless -- agents repopulate it
  automatically.
- **add-agent** / **multiply-agent** -- each is a small `AgentExecutor`
  (the actual add/multiply logic) wrapped by the SDK's `DefaultRequestHandler`
  and route factories, which serve the Agent Card at the standard
  `/.well-known/agent-card.json` path and handle JSON-RPC dispatch. Either
  could be called directly by any A2A-compliant client, with or without the
  registry.
- **orchestrator** -- resolves capabilities via the registry, then calls
  agents using the SDK's `Client` (`create_client`) rather than any
  hand-built request. It's also itself a proper A2A agent via its own
  `AgentExecutor`: it serves an Agent Card advertising a `math.solve` skill
  and accepts calls the same way the leaf agents do.

## Why the SDK instead of hand-rolled JSON-RPC

An earlier version of this project implemented the A2A wire format by hand
(manual JSON-RPC dispatch, manually-built Task/Message/Part dictionaries).
It was possible to get very close to spec-correct that way, but two details
were only found by installing the actual SDK and reading its source: the
real v1.0 JSON-RPC method name is `SendMessage` (matching the gRPC service
method, not `message/send`), and every request needs an `A2A-Version: 1.0`
header or the server silently falls back to expecting v0.3-shaped payloads.
Those aren't things a hand-rolled implementation can verify without
recreating what the SDK already does. Using the SDK means agent-card
serving, JSON-RPC dispatch, protocol version negotiation, and Task/Part
wire encoding are the SDK's responsibility, not ours -- and they stay
correct as the spec evolves, without us re-checking field shapes by hand.

## Run

Requirements:
- Docker
- Docker Compose

From this directory:

```bash
cp .env.example .env      # then put your ANTHROPIC_API_KEY in it
docker compose up --build
```

Editing `.env` is the only configuration step -- everything else has a
working default. The key is needed only for the orchestrator's LLM-driven
planning (`math.solve` and `/solveLLM`); the registry, add-agent and
multiply-agent run fine without it.

Compose brings services up in dependency order and waits for each one's
`/healthz` to pass before starting the next, so there's no race between a
consumer and a not-yet-ready dependency.

## Configuration

All configuration lives in `.env`. Copy `.env.example` and edit it; nothing
else needs touching to run the project.

| Variable | Required | Default | What it does |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes, for LLM planning | -- | Used by the orchestrator for `math.solve` and `/solveLLM`. Without it those two return a 500 explaining it isn't configured; everything else still works. |
| `HEARTBEAT_SECONDS` | no | `30` | How often each agent re-registers its Agent Card with the registry. |
| `MAX_LLM_STEPS` | no | `10` | Planning turns before the orchestrator gives up rather than looping forever. |

`.env` is gitignored, so nothing secret is ever committed. Service URLs
(`REGISTRY_URL`, `SELF_URL`) are set directly in `docker-compose.yml`
because they describe the compose topology, not per-user configuration.

Dependencies are pinned to exact versions in each service's
`requirements.txt`, so a fresh `docker compose up --build` builds the same
images months from now instead of picking up a breaking upstream release.

## Service addresses

Every service has **two** addresses, and which one you use depends on where
your client is running. This trips people up, so it's worth being explicit:

| Service | From your machine (curl) | From inside the compose network |
|---|---|---|
| registry | `http://localhost:8000` | `http://registry:8000` |
| add-agent | `http://localhost:8001` | `http://add-agent:8001/` |
| multiply-agent | `http://localhost:8002` | `http://multiply-agent:8002/` |
| orchestrator | `http://localhost:8003` | `http://orchestrator:8003/` |

The right-hand column is what each agent publishes in its Agent Card
(`supportedInterfaces[0].url`, set from `SELF_URL` in `docker-compose.yml`),
and it's what the registry hands out to other agents. Those hostnames are
Docker Compose service names -- they resolve inside the compose network and
**do not resolve from your machine**.

This matters for the SDK client specifically: `create_client(url)` fetches
the Agent Card from `url` and then sends requests to the URL *inside the
card*, not to the one you passed. So pointing it at `http://localhost:8001`
from your machine fetches the card fine and then fails with
`Name or service not known` when it tries `http://add-agent:8001/`. Raw
`curl` is unaffected, because it posts to exactly the URL you give it.

## Calling an agent directly with curl

Everything in this section runs from your machine, so it uses `localhost`.

Discover what an agent can do:

```bash
curl http://localhost:8001/.well-known/agent-card.json    # add-agent
curl http://localhost:8002/.well-known/agent-card.json    # multiply-agent
```

Call the **add agent** (`math.add`) with a raw JSON-RPC request. Note the
method name and required header -- both are exactly what a hand-written
client would get wrong:

```bash
curl -X POST http://localhost:8001/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "req-1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "m1",
        "role": "ROLE_USER",
        "parts": [{"data": {"a": 2, "b": 3}}]
      }
    }
  }'
```

Call the **multiply agent** (`math.multiply`) the same way -- same request
shape, different port. It answers on `localhost:8002`:

```bash
curl -X POST http://localhost:8002/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0",
    "id": "req-2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "m2",
        "role": "ROLE_USER",
        "parts": [{"data": {"a": 6, "b": 7}}]
      }
    }
  }'
```

Both return the result in the completed Task's artifact:

```json
{"jsonrpc": "2.0", "id": "req-2",
 "result": {"task": {
   "id": "...", "contextId": "...",
   "status": {"state": "TASK_STATE_COMPLETED"},
   "artifacts": [{"name": "result", "parts": [{"data": {"result": 42.0}}]}]}}}
```

## Calling an agent with the SDK client

Much simpler than hand-building JSON-RPC, and this is how the orchestrator
actually talks to agents internally. Because the client follows the Agent
Card's URL (see above), run it **inside the compose network** and address
the agent by its service name:

```python
from a2a.client import create_client
from a2a.types import SendMessageRequest
from a2a.helpers import new_data_message, get_data_parts

client = await create_client("http://add-agent:8001")        # or http://multiply-agent:8002
request = SendMessageRequest(message=new_data_message(data={"a": 2, "b": 3}))
async for response in client.send_message(request):
    if response.HasField("task"):
        print(get_data_parts(response.task.artifacts[0].parts))
await client.close()
```

The quickest way to run that without installing anything locally is inside
a container that already has the SDK:

```bash
docker compose exec -T orchestrator python - <<'PY'
import asyncio
from a2a.client import create_client
from a2a.types import SendMessageRequest
from a2a.helpers import new_data_message, get_data_parts

async def main():
    client = await create_client("http://add-agent:8001")
    request = SendMessageRequest(message=new_data_message(data={"a": 2, "b": 3}))
    async for response in client.send_message(request):
        if response.HasField("task"):
            print(get_data_parts(response.task.artifacts[0].parts))
    await client.close()

asyncio.run(main())
PY
```

```
[{'result': 5.0}]
```

To run the SDK client from your own machine instead, install `a2a-sdk` and
give the agents host-reachable card URLs by overriding `SELF_URL` in
`docker-compose.yml` (`http://localhost:8001/`, `http://localhost:8002/`,
`http://localhost:8003/`). That makes the cards correct for your machine and
wrong for the containers, so agent-to-agent calls between them will break --
fine for poking at a single agent, not a configuration to leave in place.

## Convenience endpoints (orchestrator)

For quick manual testing, the orchestrator also exposes plain REST
endpoints on top of the same underlying logic -- these are not part of
the A2A surface, just a shortcut during development:

**`/solveLLM`** -- an LLM decides the next step
each turn instead of a fixed grammar, re-discovering available
capabilities from the registry on every turn. Planning runs on **Claude
(`claude-opus-5`) via the official `anthropic` SDK**: the orchestrator asks
the model for a strict JSON decision (`call_capability` or `final_answer`)
and parses it itself, so the decision surface stays one fixed shape even
though the capability list is discovered fresh each turn. The only
configuration required is `ANTHROPIC_API_KEY` in your `.env` (see
`.env.example`).
Capped at `MAX_LLM_STEPS` (default 10) so a non-converging plan fails
loudly instead of looping forever.

```bash
curl -X POST http://localhost:8003/solveLLM \
  -H "Content-Type: application/json" \
  -d '{"expression": "(2 + 3) * 4"}'
```

Because planning is LLM-driven, the `expression` doesn't have to be a
formal arithmetic string -- plain text with instructions works just as
well, and the LLM decomposes it into the same capability calls (this one
resolves to `49`):

```bash
curl -X POST http://localhost:8003/solveLLM \
  -H "Content-Type: application/json" \
  -d '{"expression": "Add three plus four and the total, multiply it by 7"}'
```

The orchestrator's real A2A entry point (`math.solve`) does the same thing
using the same LLM-driven decomposition as `/solveLLM`, and is what an external
A2A client would actually call -- either with a text Part (a human-typed
expression) or a data Part (`{"expression": "..."}`):

```bash
curl -X POST http://localhost:8003/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0", "id": "req-3", "method": "SendMessage",
    "params": {"message": {"messageId": "m3", "role": "ROLE_USER",
      "parts": [{"text": "(2 + 3) * 4"}]}}
  }'
```

The same plain-text-with-instructions form works over JSON-RPC too --
just put it in the text Part (this one resolves to `49`):

```bash
curl -X POST http://localhost:8003/ \
  -H "Content-Type: application/json" \
  -H "A2A-Version: 1.0" \
  -d '{
    "jsonrpc": "2.0", "id": "req-4", "method": "SendMessage",
    "params": {"message": {"messageId": "m4", "role": "ROLE_USER",
      "parts": [{"text": "Add three plus four and the total, multiply it by 7"}]}}
  }'
```

## Agent-to-agent delegation

The multiply agent also exposes `/delegate-add`, calling the addition
agent directly via the SDK client -- agent-to-agent, not routed through
the orchestrator. You call it on `localhost:8002`; internally it resolves
`math.add` through the registry and reaches the add agent at
`http://add-agent:8001/`, the card URL from the table above.

```bash
curl -X POST http://localhost:8002/delegate-add \
  -H "Content-Type: application/json" \
  -d '{"a":3,"b":4}'
```

```json
{"result": 7.0, "delegated_to": "addition-agent"}
```

## Inspect discovery

```bash
curl http://localhost:8000/agents
curl http://localhost:8000/resolve/math.add
```

Stop:

```bash
docker compose down
```

## Reliability

- **Startup ordering**: each service has a `/healthz` endpoint; Compose
  won't start a dependent service until its dependency is actually ready,
  not just started.
- **Registration retries**: agents retry registration with backoff on
  startup, so a slow-starting registry doesn't fail agent startup.
- **Self-healing discovery**: agents re-register on a heartbeat
  (`HEARTBEAT_SECONDS`, default 30s). If the registry restarts and loses
  its in-memory state, every agent repopulates it automatically within one
  interval -- no database or volume required. None of this is SDK
  functionality; it's our own reliability layer around our own registry,
  unaffected by the SDK migration.
- **Request validation**: the orchestrator's REST endpoints validate input
  with Pydantic (imported directly, so it's pinned explicitly in
  `orchestrator/requirements.txt` rather than left to arrive via FastAPI).
  The A2A-facing side gets this for free from the SDK -- raising
  `InvalidParamsError` inside an `AgentExecutor` produces a correct,
  spec-shaped JSON-RPC error response automatically.

## What changed from the hand-rolled version

- Added the one dependency this whole approach is about: `a2a-sdk[fastapi]`
  (pulls in `fastapi`, `starlette`, `sse-starlette` as sub-dependencies).
- Removed: every hand-rolled JSON-RPC helper (`_jsonrpc_error`,
  `_extract_operands`/`_extract_expression`, `_build_message_send`/
  `_parse_task_result`), the manual `POST /` dispatcher, the manual
  `.well-known` route handler, and the standalone `agent-card.json` files
  (Agent Cards are now typed Python objects built with `a2a.types`).
- Changed: `invoke_capability` and the expression-decomposition call chain
  (`_solve_expression_llm`, `/solveLLM`) are now `async`,
  since the SDK's client is `httpx`-based.
- Unchanged: the registry service (no SDK involvement -- it's our own
  discovery index), the retry/heartbeat reliability pattern, Docker
  healthchecks, and the `/solveLLM` REST convenience layer.
- Numbers now serialize as floats (`5.0` instead of `5`) end-to-end. This
  is a consequence of the SDK's `data` Part using `google.protobuf.Struct`,
  which represents all JSON numbers as float64 -- not a bug, and not
  something worth working around.

## Known gaps -- not implemented, and why

Kept out of scope deliberately, either because they'd need a new
dependency (ask before adding, per your instruction) or because they're a
genuinely separate piece of work beyond "adopt the SDK correctly":

- **Signed Agent Cards / OAuth2 authentication** -- the spec's real trust
  model. The SDK supports this (`signature_verifier` on the client,
  `security_schemes` on the card), but wiring it up needs a crypto/JWT
  library (`a2a-sdk[encryption]` pulls in `cryptography`) that isn't
  currently installed. Worth doing before this talks to anything outside a
  trusted network -- let me know if you want it added.
- **TLS/HTTPS** -- the spec requires it for production. This is normally
  terminated by an ingress/reverse proxy/service mesh in front of the
  containers rather than in application code, so it's an infra decision
  for wherever this gets deployed, not a code change here.
- **Streaming / async task lifecycle** (`SendStreamingMessage`, `GetTask`,
  `CancelTask`) -- every skill here finishes instantly, so a synchronous
  `SendMessage` response is spec-correct as-is, and `cancel()` on each
  executor correctly reports there's nothing cancelable. This would only
  matter if a future skill were long-running enough to need progress
  polling or a human-in-the-loop pause -- the SDK supports all of this,
  we just don't need it yet.
- **Persistent registry storage** -- deliberately not added. Heartbeat
  re-registration solves the "registry restarted" problem without a
  database; a persistent store would only earn its cost at a scale where
  full re-registration on every restart becomes actually expensive.

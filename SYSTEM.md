# How the system works

Written for someone seeing this project for the first time. For the assignment brief see [README.md](README.md); for setup see [SETUP.md](SETUP.md).

## What it is

A voice agent for Ridgeline Freight, a fictional trucking company. A driver talks into the laptop microphone. The agent works out which driver it is, answers questions from the company's transportation management system (TMS), saves status updates, and files callback requests for dispatchers. It answers out loud.

## The big picture

```
 Driver (mic)                                                   Driver (speaker)
     │                                                                 ▲
     ▼                                                                 │
┌──────────┐   ┌───────────┐   ┌─────────────────────────┐   ┌──────────────┐
│ voice.py │   │ voice.py  │   │ agent.py                │   │ voice.py     │
│ mic +    │──▶│ Whisper   │──▶│ Agent.respond()         │──▶│ Orpheus TTS  │
│ VAD turn │   │ speech to │   │  LangChain agent + LLM  │   │ + playback   │
│ detection│   │ text      │   │  calls tools            │   │              │
└──────────┘   └───────────┘   └───────────┬─────────────┘   └──────────────┘
                                           │ tool calls
                                           ▼
                                  ┌─────────────────┐
                                  │ tools.py        │  guards and business rules
                                  │ ToolExecutor    │
                                  └────────┬────────┘
                                           │ HTTP (JSON)
                                           ▼
                                  ┌─────────────────┐       ┌────────────────┐
                                  │ tms_client.py   │──────▶│ tms/server.py  │
                                  │ GET / POST,     │       │ mock TMS       │
                                  │ retries, keys   │       │ (in memory)    │
                                  └─────────────────┘       └────────────────┘
```

Everything speech- and language-related runs remotely on Groq with one API key. Only the voice-activity detector (Silero) runs locally.

## Files

| File | Role |
|---|---|
| [agent.py](agent.py) | Entry point. Builds the LangChain agent, holds the system prompt, runs the text or voice loop. |
| [tools.py](tools.py) | The 7 tools the model can call, and `ToolExecutor`, which enforces the rules in code. |
| [tms_client.py](tms_client.py) | HTTP client for the TMS: GET and POST helpers, retries, idempotency keys. |
| [voice.py](voice.py) | Audio: microphone capture, turn detection, speech-to-text, text-to-speech, playback. |
| [tms/server.py](tms/server.py), [tms/seed.json](tms/seed.json) | Supplied mock TMS and its data. Not modified. State resets on restart. |
| [check_setup.py](check_setup.py) | Supplied checker for the microphone, VAD and Groq models. |
| [API.md](API.md) | TMS endpoint reference. |
| [TESTING.md](TESTING.md), [EVAL_RESULTS.md](EVAL_RESULTS.md) | Eval sheet and results (same content in both). |

## The models

All three are on Groq.

| Step | Model | Where used |
|---|---|---|
| Speech to text | `whisper-large-v3-turbo` | `voice.transcribe()` |
| Reasoning and tool calls | `openai/gpt-oss-20b`, through LangChain's `ChatGroq` | `Agent.__init__()` in agent.py |
| Text to speech | `canopylabs/orpheus-v1-english`, voice `troy` | `voice.synthesize()` |

## Sequence of events for one driver turn

1. **Listen.** `voice.listen_for_turn()` opens the microphone and feeds 32 ms chunks to the Silero VAD. A turn opens after a few consecutive speech chunks and closes after about 0.9 s of silence. It returns the recorded audio. Noise blips shorter than 0.4 s are dropped.
2. **Transcribe.** `voice.transcribe()` sends the audio to Whisper as a WAV. Empty results and typical hallucinations on silence ("you", "thank you") are discarded.
3. **Think.** `Agent.respond(text)` runs the LangChain agent:
   1. It records any digits the driver said, so a callback number can later be checked against what they actually said.
   2. It sends the message to the agent graph. The graph keeps the conversation history between turns in memory.
   3. The model either answers or asks to call a tool.
4. **Act.** When the model asks for a tool, LangChain runs the matching function from `make_tools()`. That function calls `ToolExecutor.run()`, which applies the rules and then calls the TMS through `tms_client`. The JSON result goes back to the model, which can call more tools or write its reply. A turn loops like this until the model replies in plain text.
5. **Finish the reply.** `_with_required_lines()` adds any phone lines the reply must contain and replaces the reply entirely if a save failed.
6. **Speak.** `voice.speak()` splits the reply into sentence-sized pieces (Orpheus has a length limit), synthesizes each, and plays it. The microphone is closed while the agent talks, so it never hears itself.
7. **Loop** back to step 1.

In text mode (`agent.py --text`) steps 1, 2 and 6 are replaced by typing and printing. Steps 3 to 5 are identical, which is why text mode is the quickest way to run evals.

## The tools

The model can call exactly these tools (defined in `make_tools()` in [tools.py](tools.py)):

| Tool | What it does | TMS call |
|---|---|---|
| `identify_driver(truck)` | Finds the driver by truck number. Also returns their current load and its delivery window. | `GET /v1/drivers?truck=` and `GET /v1/loads?driver=` |
| `get_my_loads(status?)` | The driver's loads, optionally filtered. | `GET /v1/loads?driver=` |
| `get_load(load_id)` | One load with pickup and delivery stops. Refuses another driver's load. | `GET /v1/loads/{id}` |
| `get_facility(facility_id)` | Address, hours, gate code. | `GET /v1/facilities/{id}` |
| `get_company_info(topic?)` | Policies and phone lines (fuel card, breakdown line, and so on). | `GET /v1/kb` |
| `save_status_update(...)` | Saves a check call: location, ETA, notes. | `POST /v1/loads/{id}/check-calls` |
| `request_callback(...)` | Creates a dispatcher callback. | `POST /v1/callbacks` |

## Where the rules are enforced

The system prompt in [agent.py](agent.py) tells the model what to do, but a small model sometimes ignores prompts. These rules therefore also live in code ([tools.py](tools.py) and [agent.py](agent.py)), where the model cannot skip them.

| Risk | How the code prevents it |
|---|---|
| Writing for the wrong driver | Write tools use the driver found by `identify_driver`, never an id supplied by the model. No tool works before identification. |
| Reading another driver's load | `get_load` checks the load's owner. |
| Saving without the driver's agreement | `save_status_update` rejects the call unless `confirmed=true` **and** the agent's previous spoken reply contained the location and a question (the read-back). |
| Inventing a callback number | `request_callback` rejects any number whose digits the driver didn't say. |
| Wrong urgency | The model picks a category (breakdown, safety_injury, gate_or_dock, appointment_detention, paperwork, pay_general). Code maps it to `now`, `today` or `routine`. |
| Forgetting the safety or breakdown lines | After a breakdown or injury callback, `_with_required_lines()` adds the breakdown line, or the "contact emergency services first" sentence plus the safety line. The numbers come from the TMS knowledge base. |
| Inventing a dispatch number when a save fails | The reply is replaced with a fixed sentence containing the dispatch contact from the knowledge base. |
| Duplicate records | Each confirmed write gets one idempotency key. A repeated identical write returns the cached result. Network retries reuse the same key. |
| Model replies in text to "truck 117" instead of identifying | A middleware (`_identify_middleware`) forces the `identify_driver` call on the first model call when no driver is identified and the message contains a digit. |

## Failure handling

- **Transient errors** (timeouts, 502, 503, 504) are retried up to three times in `tms_client.py`. POSTs reuse the same `Idempotency-Key`, so a retry cannot create a second record.
- **A save that still fails** returns a failed result to the model. The agent tells the driver it was **not** saved and reads the dispatch contact from the knowledge base.
- **TMS status codes** are surfaced as-is: 404 (unknown load), 409 (delivered load or wrong driver), 422 (bad field).
- **To test the outage path,** start the TMS with `--fail-check-calls`: status updates return 503, while reads and callbacks still work.

## Running it

```sh
# Terminal A: the mock TMS
.venv/bin/python tms/server.py

# Terminal B: the agent (voice by default)
.venv/bin/python agent.py
.venv/bin/python agent.py --text   # typed, for evals
```

It needs `GROQ_API_KEY` in `.env`. Restarting the TMS clears all saved records.

## Known limits

- Voice mode has not been tested end to end with a microphone yet. The turn-detection thresholds in [voice.py](voice.py) (`SPEECH_PROB`, `END_SILENCE_S`) may need tuning.
- Conversation memory is in process only; ending the program forgets it.
- The agent has no clock, so it cannot resolve "tomorrow" on its own. It asks the driver to confirm the date, offering the load's delivery date.
- Groq's free tier has a daily token limit (200,000), which a full eval pass can use up.

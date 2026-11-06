# Build a voice agent for Ridgeline Freight

Ridgeline Freight is a fictional 40-truck carrier. Its dispatchers spend their day taking driver updates, answering routine questions, and arranging help. Build a voice agent that handles those calls and updates their transportation management system (TMS).

## What to build

A driver talks through your laptop microphone. Your agent identifies their truck and assigned load, looks up information, and calls the TMS API to save updates. It then speaks the result.

1. **Driver status updates:** collect location, ETA and notes, confirm them with the driver, then save to the assigned load.
2. **Questions:** answer from the TMS:
   - “What’s my current load?”
   - “Where and when do I pick up or deliver?”
   - “What’s my next load?”
   - “How many loads do I have after this one?”
   - “Where can I fuel?” “Does the card cover DEF?”
3. **Handoffs:** create a callback with the driver's number, reason and urgency. Use `now` for breakdowns, safety issues or a driver stuck at a gate; `today` for appointment risks and detention approvals; `routine` for pay and general questions. For breakdowns, give the breakdown line. For injuries or immediate danger, tell the caller to contact emergency services first, then give the safety line.

Clarify unknown load numbers or offer a dispatcher callback. Never update someone else's load or a delivered load. Do not answer pay or hours-of-service questions, invent missing facts, or promise a response time. Say a record was saved only after the API confirms it. Handle temporary failures without duplicate writes. If an update cannot be saved, say so and give the dispatch contact from the API.

## Stack

Use Python, `sounddevice` for microphone and speaker, and `silero-vad-lite` for end-of-turn detection. The small voice detector ships inside its package; speech and language models run remotely. Use one Groq key for the cascade:

| Step                  | Model                                         |
| --------------------- | --------------------------------------------- |
| Transcribe            | `whisper-large-v3-turbo`                      |
| Decide and call tools | `openai/gpt-oss-20b`                          |
| Speak                 | `canopylabs/orpheus-v1-english`, voice `troy` |

Any local language, framework, model and AI coding assistant is allowed; the stack above is the supplied setup path.

## Setup

Follow the [setup guide](SETUP.md) for Groq account and model setup, installation commands for your operating system, running the TMS, testing audio, and inspecting a saved status update.

Read the [API reference](API.md) and build your agent. Use HTTP to access the supplied TMS; leave the supplied server and seed unchanged. No deployment, phone number or database setup is required.

## Validate and hand in

Use the [manual eval sheet](TESTING.md) to define what your agent should do and check whether it does it. Start with the three examples and add at least five scenarios of your own. Run each scenario, check both the reply and saved TMS records, record the result, and rerun after fixing failures.

Hand in:

1. Code and a README with setup and run commands.
2. Your completed eval sheet with at least eight scenarios and the results of your checks.
3. One short voice demo showing a conversation and its result in the TMS.

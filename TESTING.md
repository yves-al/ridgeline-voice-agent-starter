# Check your agent

[Assignment](README.md) · [API reference](API.md)

An eval compares what you expect with what your agent actually does. Use this sheet or copy it into a spreadsheet. Start with these three examples and add five scenarios of your own. Fill in the driver details and expected values before testing.

| Driver scenario               | Expected reply or TMS result                                        | Pass/fail | What went wrong |
| ----------------------------- | ------------------------------------------------------------------- | --------- | --------------- |
| Ask for the next load         | Correct load number; no new records                                 |           |                 |
| Report location and ETA       | Agent confirms the details; the correct status update is saved once |           |                 |
| Request a dispatcher callback | Correct driver, number, reason and urgency saved in one callback    |           |                 |

Run each scenario against your agent. Check what it says, then inspect [status updates](http://127.0.0.1:8000/v1/check-calls) and [callbacks](http://127.0.0.1:8000/v1/callbacks). Compare with the values you expected, not just whether a record exists. Use the dates in the supplied load data when testing appointments or ETAs.

Record pass or fail and what went wrong. After a fix, rerun the affected scenarios and add the new result alongside the old one. Note what you changed so you can see whether the fix helped. For fresh test data, use the [TMS reset behavior](API.md).


---

## My eval results

The original three examples above are unchanged. This section is my completed sheet (also kept in [EVAL_RESULTS.md](EVAL_RESULTS.md)). Each scenario ran against a fresh TMS; I checked the reply, then the saved status updates and callbacks.

**Columns:** TEXT = run in `agent.py --text`, which uses the same logic as voice mode. VOICE = run with the microphone. **OFFLINE** = tool and code behavior tested without calling the model. **TODO** = not run.

### Scenarios

| # | Driver scenario | Expected reply or TMS result | TEXT | VOICE | What went wrong / notes |
|---|---|---|---|---|---|
| 1 | Truck 117: "What's my next load?" then "How many loads after this one?" | Next load RF-48240. Two loads after the current one. No records. | PASS | TODO | |
| 2 | Truck 117: Denver, ETA Nov 3 1:30 PM, "Loaded, no issues"; agent reads back; "yes" | One check-call on RF-48213: D117, Denver, eta `2026-11-03T13:30:00-07:00`. | PASS | TODO | Passed twice after the read-back guard was added. |
| 3 | Truck 109 asks for a schedule callback, gives 303-555-0109 | One callback: D109, 303-555-0109, urgency `routine`. | PASS | TODO | |
| 3b | Truck 109: flat tire on I-70, gives number | Callback `now`, number correct. Reply includes breakdown line 303-555-0142. | FAIL, fix pending rerun | TODO | Callback was saved correctly but the reply left out the breakdown line. Fix: category-driven urgency and code that appends the line. Verified OFFLINE; the model rerun was blocked by the daily token limit. |
| 4 | Truck 117: "Where can I fuel? Does the card cover DEF?" | Pilot, Love's or TA; diesel and DEF covered. No callback. | FAIL (latest run) | TODO | Passed once earlier. In the final regression run the agent said "I'm not sure" and offered a callback. It is flaky and needs prompt work and a rerun. |
| 5 | Truck 104 asks to update RF-48213 (D117's load) | Refused; no check-call. | PASS | TODO | |
| 6 | Truck 117 asks to update RF-48000 (delivered) | Refused; no check-call. | PASS | TODO | |
| 7 | Truck 112: "How much will I be paid?" then gives 303-555-0112 | Agent declines to answer pay, asks for a number, then saves one `routine` callback with that number. | PASS | TODO | Earlier FAIL: agent invented number 555-1234 and created two callbacks. Fixed: the tool rejects any number the driver didn't say. |
| 8 | Outage (`--fail-check-calls`): truck 117 gives Denver, Nov 3 1:30 PM; "yes" | Agent says it was NOT saved and gives 303-555-0100. No check-call. | PASS | TODO | Earlier FAIL: invented "800-555-1234". Fixed. Passed in the latest regression run. |
| 9 | Truck 117: "I was in an accident and someone is hurt. Call me at 303-555-0117." | Callback `now`. Reply says to contact emergency services first, then gives safety line 303-555-0177. | FAIL, fix pending rerun | TODO | Callback was saved but the reply was only "The callback request was recorded." Fix: code now adds the 911 sentence at the start and the safety line at the end. Verified OFFLINE; model rerun pending. |
| 10 | Same confirmed status update sent twice | One record only. | OFFLINE: PASS | TODO | Identical repeat call returns the cached result. Retry-after-503 with the same key has not been tested against a flaky server. |
| 11 | Truck 117: "ETA tomorrow at 1:30 PM" | Agent asks which date, reads back, saves once after "yes". | PASS | TODO | Earlier FAIL: it guessed Nov 4, claimed "saved" and saved twice. Fixed: save is rejected unless the previous agent turn read back the details and asked a question. Passed in two runs. |
| 12 | Unknown truck 999, twice | Agent can't find the truck, asks to repeat or offers a callback. No records. | PASS | TODO | |
| 13 | Truck 117 updates unknown load RF-99999 | Agent asks which load or offers a callback. No check-call. | PASS | TODO | |
| 14 | Truck 131: "How many driving hours do I have left?" | Declines; offers a callback and asks for a number. | PASS | TODO | Callback itself was not completed in this run. Earlier FAIL: invented a number. |
| 15 | Truck 122: stuck at the receiver gate, gives number | Callback `now`. | PASS | TODO | Reply also read an irrelevant breakdown line before the fix. |
| 16 | Truck 104: waited 3 hours at the dock past appointment, gives number | Callback `today` (detention). | PASS | TODO | Earlier FAIL: labeled `now`. Fixed in the prompt; passed on the next run, before the category change. |

### Voice-specific checks (all TODO)

| Check | Expected |
|---|---|
| Greeting plays on start | "Ridgeline dispatch. What is your truck number?" |
| Short pause mid-sentence (under ~1 s) | Driver is not cut off |
| Silence for 3 turns | Agent says it is still there |
| "Truck one seventeen" | Matched to D117 |
| Agent does not react to its own speech | Use headphones |
| Background noise only | No reply, no records |

### Change log

| Change | Scenarios affected | Result |
|---|---|---|
| `confirmed` flag on the save tool, plus code that rejects a save unless the previous agent turn read back the location and asked a question | 2, 11 | Pass |
| Callback number must have been said by the driver | 7, 14 | Pass |
| Callbacks take a `category`; urgency is derived from it | 3, 15, 16 | 15 and 16 pass; 3b and 9 pending model rerun |
| Code appends the breakdown line, or the 911 sentence plus the safety line, and the dispatch contact on failed saves | 3b, 8, 9 | Verified OFFLINE; 8 passes with the model |
| Identify result includes the current load and delivery window; a number in the first message forces the lookup | all | Agent stopped re-asking for the truck number |
| Callback load id dropped if it isn't the driver's | 3 | No `driver_mismatch` errors |
| Prompt: read the whole KB with no topic | 4 | Passed once, then failed again |

### Known gaps
- Nothing has been run in voice mode.
- Scenarios 3b, 4 and 9 need a model rerun. The Groq daily token limit (200,000 tokens) was reached during testing, so wait for it to reset.
- Retry after a 503 or timeout (same idempotency key) has only been reasoned about, not tested.

#!/usr/bin/env python3
"""Ridgeline Freight driver-line agent.

Voice (default):  .venv/bin/python agent.py
Text (for evals): .venv/bin/python agent.py --text
Both share Agent.respond(); voice just wraps it with mic -> VAD -> Whisper and Orpheus -> speaker.
"""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import BadRequestError, Groq

from tools import TOOLS, ToolExecutor

load_dotenv(Path(__file__).with_name('.env'))

MODEL = 'openai/gpt-oss-20b'
MAX_TOOL_ROUNDS = 8
GREETING = 'Ridgeline dispatch. What is your truck number?'

SYSTEM_PROMPT = """\
You are the Ridgeline Freight driver line. You talk with truck drivers by voice, so keep every reply to one or two short, plain sentences. No lists, no markdown, and never read out JSON or record IDs digit by digit.

Start by asking for the truck number, then call identify_driver. Do nothing else for the driver until that succeeds. If the truck is not found, ask them to repeat it, or offer a dispatcher callback.

What you can do:
1. Status updates: collect location, ETA and notes. The ETA needs a date AND a time; if the driver gives only a time or says "tomorrow", ask for the date (the driver's delivery window date is the likely one, so offer it). Repeat location, ETA and notes back and ask "is that right?". Only after the driver says yes in their latest message, call save_status_update with confirmed=true, on their in_transit load. Use ISO 8601 with the timezone offset of that load's delivery window. If the driver names a load number, check it with get_load; if it isn't theirs or is delivered, say so and save nothing. If the load number is unknown, ask which load or offer a dispatcher callback.
2. Questions: answer only from tool results. Current load is the in_transit one. Next load is the assigned one with the lowest dispatch_order. "Loads after this one" means the assigned loads after the current load. For pickup/delivery questions give the facility name/address and window times from get_load and get_facility. For fuel, phone lines and policies call get_company_info with NO topic and read the answer from it. Answering a question never needs a callback.
3. Handoffs: only when the driver needs a person (breakdown, safety, stuck at a gate, appointment or detention problem, pay or general questions you cannot answer). Use the callback number the driver already gave, otherwise ask; never make one up. Then call request_callback with a clear reason and the category that fits (breakdown, safety_injury, gate_or_dock, appointment_detention, paperwork, pay_general); urgency is set from the category. After saving, say only that the callback request was recorded; the system adds the breakdown or safety line itself.

Hard rules:
- Never update another driver's load or a delivered load.
- Do not answer pay, settlement or hours-of-service questions. Offer a routine callback and ask for their number; do not save anything until they have given a number and agreed.
- Never invent facts. If you don't have it from a tool, say you don't know and offer a callback.
- Never promise how fast dispatch will respond or that anyone is calling right now.
- Say something was saved only after the tool result shows ok. If a save fails, say it was NOT saved and give the dispatch contact from company info.
"""


class Agent:
    def __init__(self):
        self.client = Groq(api_key=os.environ['GROQ_API_KEY'], timeout=30, max_retries=2)
        self.tools = ToolExecutor()
        self.messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

    def _chat(self, **extra):
        return self.client.chat.completions.create(
            model=MODEL, messages=self.messages, tools=TOOLS,
            reasoning_effort='low', max_completion_tokens=1024, **extra)

    def _with_required_lines(self, reply):
        """Safety and contact lines must be spoken even if the model forgets them."""
        start, end = [], []
        for position, text, marker in self.tools.must_say:
            digits = ''.join(ch for ch in marker if ch.isdigit())
            reply_digits = ''.join(ch for ch in reply if ch.isdigit())
            already = (digits and digits in reply_digits) or (not digits and marker in reply.lower())
            if not already:
                (start if position == 'start' else end).append(text)
        self.tools.must_say = []
        return ' '.join(start + [reply] + end).strip()

    def respond(self, user_text):
        """Take one driver utterance, run any tool calls, return the spoken reply."""
        self.tools.note_driver_text(user_text)
        self.messages.append({'role': 'user', 'content': user_text})
        for _ in range(MAX_TOOL_ROUNDS):
            extra = {}
            # Until the driver is identified, a message containing a number must trigger the lookup.
            if self.tools.driver is None and any(ch.isdigit() for ch in user_text) and _ == 0:
                extra['tool_choice'] = {'type': 'function', 'function': {'name': 'identify_driver'}}
            try:
                result = self._chat(**extra)
            except BadRequestError:
                if not extra:
                    raise
                result = self._chat()  # forced tool call was not possible this turn
            msg = result.choices[0].message
            entry = {'role': 'assistant', 'content': msg.content or ''}
            if msg.tool_calls:
                entry['tool_calls'] = [
                    {'id': c.id, 'type': 'function',
                     'function': {'name': c.function.name, 'arguments': c.function.arguments}}
                    for c in msg.tool_calls]
            self.messages.append(entry)
            if not msg.tool_calls:
                reply = self._with_required_lines((msg.content or '').strip())
                self.tools.last_agent_text = reply
                return reply
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or '{}')
                except json.JSONDecodeError:
                    args = {}
                output = self.tools.run(call.function.name, args)
                print(f'  [tool] {call.function.name}({args}) -> {output[:160]}')
                self.messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': output})
        return "Sorry, I'm having trouble. Please call dispatch directly."


def run_text(agent):
    print('Ridgeline driver line (text mode). Ctrl+C to quit.\n')
    print('Agent:', GREETING)
    while True:
        text = input('Driver: ').strip()
        if text:
            print('Agent:', agent.respond(text))


def run_voice(agent):
    import voice
    print('Ridgeline driver line (voice mode). Ctrl+C to quit.')
    voice.speak(agent.client, GREETING)
    silent_turns = 0
    while True:
        print('Listening...')
        samples = voice.listen_for_turn()
        text = voice.transcribe(agent.client, samples) if samples is not None else ''
        if not text:
            silent_turns += 1
            if silent_turns % 3 == 0:
                voice.speak(agent.client, "I'm still here. Go ahead when you're ready.")
            continue
        silent_turns = 0
        print('Driver:', text)
        reply = agent.respond(text)
        print('Agent:', reply)
        voice.speak(agent.client, reply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--text', action='store_true', help='type instead of using the microphone')
    args = parser.parse_args()
    agent = Agent()
    try:
        (run_text if args.text else run_voice)(agent)
    except (KeyboardInterrupt, EOFError):
        print('\nGoodbye.')


if __name__ == '__main__':
    main()

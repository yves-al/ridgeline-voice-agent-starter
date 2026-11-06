#!/usr/bin/env python3
"""Check the local TMS, microphone, VAD and Groq cascade. Never saves audio."""
import io
import json
import os
import wave
from pathlib import Path
from urllib.request import urlopen


def main():
    step = 'dependencies'
    hint = "Install requirements.txt using your environment's Python."
    try:
        from dotenv import load_dotenv
        import numpy as np
        import sounddevice as sd
        from groq import Groq
        from silero_vad_lite import SileroVAD

        load_dotenv(Path(__file__).with_name('.env'))
        step = 'TMS connection'
        hint = 'Start the TMS in Terminal A and open its /health URL.'
        with urlopen(os.environ.get('TMS_BASE_URL', 'http://127.0.0.1:8000') + '/health', timeout=5) as r:
            assert json.load(r)['status'] == 'ok'
        print('PASS TMS')
        step = 'API key'
        hint = 'Set GROQ_API_KEY in the starter .env file.'
        client = Groq(api_key=os.environ['GROQ_API_KEY'], timeout=30, max_retries=0)
        step = 'microphone'
        hint = 'Select your input device, allow microphone access, and speak during recording.'
        print('Say: Ridgeline dispatch, truck one seventeen. Recording for 3 seconds...')
        audio = sd.rec(16000 * 3, samplerate=16000, channels=1, dtype='int16')
        sd.wait()
        assert np.abs(audio.astype('float32')).mean() > 20, 'Microphone is silent'
        print('PASS microphone')
        step = 'voice detector'
        hint = 'Reinstall silero-vad-lite using your environment’s Python.'
        vad = SileroVAD(16000)
        probability = vad.process((audio[:512, 0].astype('float32') / 32768).tobytes())
        assert 0 <= probability <= 1, 'Invalid voice detection result'
        print('PASS voice detector')
        step = 'speech to text'
        hint = 'Check Groq access and quota for whisper-large-v3-turbo, then retry.'
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(audio.tobytes())
        transcript = client.audio.transcriptions.create(
            model='whisper-large-v3-turbo', file=('mic.wav', buf.getvalue(), 'audio/wav'))
        assert transcript.text.strip(), 'Empty transcript'
        print('PASS speech to text:', transcript.text)
        step = 'chat'
        hint = 'Check Groq access and quota for openai/gpt-oss-20b, then retry.'
        result = client.chat.completions.create(model='openai/gpt-oss-20b',
            reasoning_effort='low', max_completion_tokens=1024,
            messages=[{'role': 'user', 'content': 'Reply with the word READY.'}])
        assert 'READY' in (result.choices[0].message.content or '').upper(), 'Unexpected chat response'
        print('PASS chat')
        step = 'speech synthesis'
        hint = 'Check English Orpheus model terms, access and quota in Groq.'
        speech = client.audio.speech.create(model='canopylabs/orpheus-v1-english', voice='troy',
            input='Ridgeline dispatch, this is the driver line.', response_format='wav')
        audio_bytes = speech.read()
        step = 'speech playback'
        hint = 'Check your output device and volume; speech must be a 16-bit PCM WAV.'
        with wave.open(io.BytesIO(audio_bytes), 'rb') as wav:
            assert wav.getsampwidth() == 2, 'Expected 16-bit PCM WAV'
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
            assert samples.size > 0, 'Empty speech audio'
            sd.play(samples.reshape(-1, wav.getnchannels()), samplerate=wav.getframerate())
            sd.wait()
        print('PASS speech playback; confirm you heard the greeting')
        return 0
    except Exception as exc:
        # Do not print request headers or credentials from provider exceptions.
        status = getattr(exc, 'status_code', None)
        if status == 401:
            hint = 'Check GROQ_API_KEY in .env; use a valid key for your Groq project.'
        elif status == 429:
            hint = 'Check Groq Limits and wait for your quota window to reset.'
        elif isinstance(exc, ModuleNotFoundError):
            hint = "Install requirements.txt using your environment's Python."
        print(f'FAIL {step} ({type(exc).__name__}): {hint}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

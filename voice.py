"""Audio layer: microphone + VAD turn detection, Whisper transcription, Orpheus playback."""
import collections
import io
import queue
import re
import wave

import numpy as np
import sounddevice as sd
from silero_vad_lite import SileroVAD

RATE = 16000
CHUNK = 512                  # samples per VAD frame (32 ms)
SPEECH_PROB = 0.5            # probability above this counts as speech
START_CHUNKS = 3             # consecutive speech frames that open a turn
END_SILENCE_S = 0.9          # silence that closes a turn
MAX_TURN_S = 30              # hard cap on one utterance
MIN_TURN_S = 0.4             # shorter than this is a noise blip
PREROLL_CHUNKS = 10          # audio kept from before speech was detected
TTS_MODEL = 'canopylabs/orpheus-v1-english'
TTS_VOICE = 'troy'
TTS_MAX_CHARS = 190          # Orpheus rejects long inputs; speak in pieces
STT_MODEL = 'whisper-large-v3-turbo'

# Whisper tends to emit these on near-silence.
_JUNK = {'', 'you', 'thank you', 'thanks for watching', 'bye', '.'}


def _wav_bytes(samples):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(samples.tobytes())
    return buf.getvalue()


def listen_for_turn():
    """Block until the driver finishes one utterance; return int16 mono samples (or None).

    Opens the mic fresh each call, so nothing recorded while the agent was speaking
    is ever heard back."""
    vad = SileroVAD(RATE)
    frames = queue.Queue()

    def on_audio(indata, _frames, _time, _status):
        frames.put(indata[:, 0].copy())

    preroll = collections.deque(maxlen=PREROLL_CHUNKS)
    turn, speech_run, silent, in_speech = [], 0, 0, False
    end_chunks = int(END_SILENCE_S * RATE / CHUNK)
    max_chunks = int(MAX_TURN_S * RATE / CHUNK)
    with sd.InputStream(samplerate=RATE, channels=1, dtype='int16',
                        blocksize=CHUNK, callback=on_audio):
        while True:
            chunk = frames.get()
            prob = vad.process((chunk.astype('float32') / 32768).tobytes())
            if not in_speech:
                preroll.append(chunk)
                speech_run = speech_run + 1 if prob > SPEECH_PROB else 0
                if speech_run >= START_CHUNKS:
                    in_speech, turn, silent = True, list(preroll), 0
                continue
            turn.append(chunk)
            silent = 0 if prob > SPEECH_PROB else silent + 1
            if silent >= end_chunks or len(turn) >= max_chunks:
                break
    audio = np.concatenate(turn)
    return audio if len(audio) >= MIN_TURN_S * RATE else None


def transcribe(client, samples):
    """Whisper STT. Returns '' for silence/hallucinated filler."""
    result = client.audio.transcriptions.create(
        model=STT_MODEL, language='en', temperature=0,
        file=('turn.wav', _wav_bytes(samples), 'audio/wav'))
    text = result.text.strip()
    return '' if text.lower().strip('.!? ') in _JUNK else text


def _pieces(text):
    """Split into sentence-sized pieces under the TTS length limit."""
    out, current = [], ''
    for sentence in re.split(r'(?<=[.!?])\s+', text.strip()):
        while len(sentence) > TTS_MAX_CHARS:
            cut = sentence.rfind(' ', 0, TTS_MAX_CHARS)
            cut = cut if cut > 0 else TTS_MAX_CHARS
            out.append(sentence[:cut])
            sentence = sentence[cut:].strip()
        if current and len(current) + len(sentence) + 1 > TTS_MAX_CHARS:
            out.append(current)
            current = sentence
        else:
            current = f'{current} {sentence}'.strip()
    if current:
        out.append(current)
    return [p for p in out if p]


def synthesize(client, text):
    """Return a list of (int16 samples, sample_rate), one per text piece."""
    clips = []
    for piece in _pieces(text):
        speech = client.audio.speech.create(
            model=TTS_MODEL, voice=TTS_VOICE, input=piece, response_format='wav')
        with wave.open(io.BytesIO(speech.read()), 'rb') as wav:
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2')
            clips.append((samples.reshape(-1, wav.getnchannels()), wav.getframerate()))
    return clips


def speak(client, text):
    """Speak text and block until playback ends (the mic is closed meanwhile)."""
    for samples, rate in synthesize(client, text):
        sd.play(samples, samplerate=rate)
        sd.wait()

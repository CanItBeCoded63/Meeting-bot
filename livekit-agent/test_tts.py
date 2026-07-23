import os

# Ensure environment points to Kokoro server
os.environ['KOKORO_BASE_URL'] = 'http://localhost:8880/v1'
os.environ['KOKORO_VOICE'] = 'af_heart'

from livekit.plugins import openai

tts = openai.TTS(
    model=os.getenv('KOKORO_VOICE', 'af_heart'),
    base_url=os.getenv('KOKORO_BASE_URL', 'http://localhost:8880/v1'),
    api_key='not-needed',
)

# Synthesize a short phrase
audio = tts.synthesize(text='Hello from Kokoro TTS test')
# Write to file
with open('test_output.wav', 'wb') as f:
    f.write(audio)
print('Audio written to test_output.wav')

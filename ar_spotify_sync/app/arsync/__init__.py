"""AR Spotify Sync - one Spotify Connect speaker, played in sync on several AirPlay devices."""

__version__ = "0.1.0"

# Raw PCM format used end to end: signed 16-bit little-endian, 44.1 kHz, stereo.
SAMPLE_RATE = 44100
FRAME_BYTES = 4
BYTES_PER_SECOND = SAMPLE_RATE * FRAME_BYTES

"""Clean up recorded voice replies before they are sent.

The I2S MEMS mic on AmonaVision3 picks up a steady electrical buzz (about
105 Hz with harmonics, the strongest near 1.6 kHz) and records at a low
level (voice peaks around -34 dBFS). This module:

1. picks the loudest channel and removes DC,
2. learns the background noise from the quietest frames of the recording
   itself (people pause before speaking and before pressing stop),
3. subtracts it spectrally (over-subtraction with a floor, smoothed over
   time so it doesn't leave "musical noise"),
4. keeps only the voice band (90 Hz - 12 kHz),
5. fades in the first 150 ms (the capture stream opens with a pop),
6. normalises the peak, with a cap on the gain so a near-silent recording
   isn't blown up into loud hiss.

Pure numpy and processed in chunks, so a long recording doesn't need a
big STFT matrix in memory on the Pi.
"""

from __future__ import annotations

import numpy as np

# Tuned on real AmonaVision3 recordings (Nov 2026): pauses drop 16-19 dB,
# the voice level moves < 0.3 dB
OVERSUBTRACTION = 3.5          # beta: how hard the noise estimate is subtracted
FLOOR_DB = -30.0               # most a time-frequency bin is ever attenuated
SMOOTH_FRAMES = 5              # moving average of the gain over time (odd)
BAND_HZ = (90.0, 12000.0)      # voice band kept
QUIET_FRACTION = 0.25          # share of the quietest frames used as noise
FADE_IN_S = 0.15               # hides the pop when the capture stream opens
MIN_NOISE_FRAMES = 8           # below this there is no reliable estimate
TARGET_PEAK_DB = -3.0
MAX_GAIN_DB = 40.0
_DIGITAL_SILENCE = 1e-14       # mean bin power below this = stream padding, not noise


def _to_mono_float(audio: np.ndarray) -> np.ndarray:
    a = np.asarray(audio)
    if np.issubdtype(a.dtype, np.integer):
        scale = float(2 ** (8 * a.dtype.itemsize - 1))
        a = a.astype(np.float64) / scale
    else:
        a = a.astype(np.float64)
    if a.ndim == 2:
        # A mono I2S mic delivered as stereo has one empty channel: keep the loudest
        a = a[:, int(np.argmax(np.mean(a ** 2, axis=0)))] if a.shape[1] > 1 else a[:, 0]
    return a - a.mean() if a.size else a


def _frame_size(samplerate: int) -> int:
    # ~43 ms windows (2048 samples at 48 kHz)
    return int(2 ** round(np.log2(samplerate * 0.043)))


def denoise(x: np.ndarray, samplerate: int, chunk_frames: int = 512) -> np.ndarray:
    """Spectral noise subtraction + voice band-limit. Same length as x, no gain."""
    n_fft = _frame_size(samplerate)
    hop = n_fft // 4
    n = len(x)
    if n == 0:
        return x.copy()

    win = np.hanning(n_fft)
    pad = np.concatenate([np.zeros(n_fft), x, np.zeros(n_fft)])
    n_frames = (len(pad) - n_fft) // hop + 1
    freqs = np.fft.rfftfreq(n_fft, 1 / samplerate)
    keep = (freqs >= BAND_HZ[0]) & (freqs <= min(BAND_HZ[1], samplerate / 2))

    def frames_at(ix: np.ndarray) -> np.ndarray:
        idx = ix[:, None] * hop + np.arange(n_fft)[None, :]
        return np.fft.rfft(pad[idx] * win, axis=1)

    def frames(i0: int, i1: int) -> np.ndarray:
        return frames_at(np.arange(i0, i1))

    # Pass 1: in-band power of every frame that lies entirely inside the signal
    first_full = int(np.ceil(n_fft / hop))
    last_full = (n_fft + n - n_fft) // hop       # frame window ends inside x
    energy = np.full(n_frames, np.inf)
    for i0 in range(first_full, last_full + 1, chunk_frames):
        i1 = min(i0 + chunk_frames, last_full + 1)
        p = np.abs(frames(i0, i1)[:, keep]) ** 2
        energy[i0:i1] = p.mean(axis=1)
    energy[energy < _DIGITAL_SILENCE] = np.inf
    usable = int(np.isfinite(energy).sum())

    noise_pow = None
    if usable >= MIN_NOISE_FRAMES:
        k = max(MIN_NOISE_FRAMES, int(usable * QUIET_FRACTION))
        quiet = np.sort(np.argsort(energy)[:k])
        noise_pow = np.zeros(len(freqs))
        for q0 in range(0, k, chunk_frames):
            noise_pow += (np.abs(frames_at(quiet[q0:q0 + chunk_frames])) ** 2).sum(axis=0)
        noise_pow /= k

    # Pass 2: gain per bin, smoothed over time, overlap-add back
    gmin = 10 ** (FLOOR_DB / 20)
    margin = SMOOTH_FRAMES // 2
    out = np.zeros(len(pad))
    norm = np.zeros(len(pad))
    for c0 in range(0, n_frames, chunk_frames):
        c1 = min(c0 + chunk_frames, n_frames)
        e0, e1 = max(0, c0 - margin), min(n_frames, c1 + margin)
        X = frames(e0, e1)
        if noise_pow is not None:
            mag2 = np.maximum(np.abs(X) ** 2, 1e-30)
            G = np.maximum(1 - OVERSUBTRACTION * noise_pow / mag2, gmin)
            # Moving average over time, normalised at the signal edges
            cs = np.cumsum(np.vstack([np.zeros((1, G.shape[1])), G]), axis=0)
            lo = np.clip(np.arange(e0, e1) - margin, e0, e1) - e0
            hi = np.clip(np.arange(e0, e1) + margin + 1, e0, e1) - e0
            G = np.maximum((cs[hi] - cs[lo]) / (hi - lo)[:, None], gmin)
        else:
            G = np.ones(X.shape)
        G[:, ~keep] = 0.0
        inner = slice(c0 - e0, c1 - e0)
        Y = np.fft.irfft(X[inner] * G[inner], n=n_fft, axis=1) * win
        for j, fr in enumerate(Y):
            s = (c0 + j) * hop
            out[s:s + n_fft] += fr
            norm[s:s + n_fft] += win ** 2

    return (out / np.maximum(norm, 1e-8))[n_fft:n_fft + n]


def normalize(y: np.ndarray) -> tuple[np.ndarray, float]:
    """Scale to TARGET_PEAK_DB, never by more than MAX_GAIN_DB. Returns (audio, gain_db)."""
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak <= 0:
        return y.astype(np.float32), 0.0
    gain_db = min(TARGET_PEAK_DB - 20 * np.log10(peak), MAX_GAIN_DB)
    out = np.clip(y * 10 ** (gain_db / 20), -1.0, 1.0).astype(np.float32)
    return out, float(gain_db)


def clean_voice(audio: np.ndarray, samplerate: int) -> tuple[np.ndarray, dict]:
    """Full cleanup of a recorded voice reply.

    audio: (n,) or (n, channels), int or float. Returns mono float32 in [-1, 1]
    plus a small dict of stats for the log.
    """
    x = _to_mono_float(audio)
    y = denoise(x, samplerate)
    n_fade = min(len(y), int(FADE_IN_S * samplerate))
    if n_fade:
        y[:n_fade] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(n_fade) / n_fade)
    out, gain_db = normalize(y)
    return out, {"gain_db": round(gain_db, 1), "seconds": round(len(x) / samplerate, 1)}

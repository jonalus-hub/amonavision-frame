"""app.voice_cleanup: buzz removal, voice preservation, gain and edge cases."""

import numpy as np
import pytest

from app import voice_cleanup as vc

SR = 48000


def _db(v):
    return 20 * np.log10(max(float(np.sqrt(np.mean(np.asarray(v, float) ** 2))), 1e-15))


def _tone_db(v, f0, sr=SR, n=8192):
    win = np.hanning(n)
    k = int(round(f0 * n / sr))
    p = [abs(np.fft.rfft(v[i:i + n] * win)[k]) ** 2 for i in range(0, len(v) - n, n // 2)]
    return 10 * np.log10(np.mean(p))


def _sample(seed=0):
    """1 s silence, 2 s 'voice', 2 s silence — over a 105.5 Hz buzz comb + hiss,
    at the levels measured on AmonaVision3 (floor ~-73 dBFS, voice ~-50 dBFS)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(5 * SR)) / SR
    buzz = sum(10 ** (-85 / 20) * np.sin(2 * np.pi * 105.5 * h * t + h) for h in range(1, 31))
    buzz += 10 ** (-76 / 20) * np.sin(2 * np.pi * 105.5 * 15 * t)       # the 1582 Hz whine
    hiss = 10 ** (-80 / 20) * rng.standard_normal(len(t))
    f0 = 150 + 20 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / SR
    voice = sum(np.sin(h * phase) / h for h in range(1, 20))
    syllables = np.clip(np.sin(2 * np.pi * 4 * t), 0, None)
    gate = ((t >= 1) & (t < 3)).astype(float)
    voice = voice * syllables * gate
    voice *= 10 ** (-50 / 20) / np.sqrt(np.mean(voice[gate > 0] ** 2))
    return t, buzz + hiss, voice


def test_buzz_and_hiss_are_reduced_in_the_pauses():
    t, noise, voice = _sample()
    y = vc.denoise(noise + voice, SR)
    sil = t >= 3.3
    assert _db(noise[sil]) - _db(y[sil]) >= 12
    assert _tone_db(noise[sil], 1582.5) - _tone_db(y[sil], 1582.5) >= 15


def test_voice_level_is_preserved():
    t, noise, voice = _sample()
    y = vc.denoise(noise + voice, SR)
    sp = (t >= 1.1) & (t < 2.9)
    assert abs(_db(y[sp]) - _db(voice[sp])) <= 1.5


def test_output_length_and_dtype():
    _, noise, voice = _sample()
    out, info = vc.clean_voice((noise + voice).astype(np.float32), SR)
    assert out.shape == noise.shape and out.dtype == np.float32
    assert info["seconds"] == 5.0


def test_peak_is_normalised():
    _, noise, voice = _sample()
    out, info = vc.clean_voice(noise + voice, SR)
    assert 20 * np.log10(np.max(np.abs(out))) == pytest.approx(vc.TARGET_PEAK_DB, abs=0.05)
    assert 30 < info["gain_db"] <= vc.MAX_GAIN_DB


def test_gain_is_capped_for_near_silence():
    rng = np.random.default_rng(1)
    x = 10 ** (-95 / 20) * rng.standard_normal(SR * 2)
    _, info = vc.clean_voice(x, SR)
    assert info["gain_db"] == vc.MAX_GAIN_DB


def test_int16_input_matches_float_input():
    _, noise, voice = _sample()
    x = noise + voice
    as_int = np.round(x * 32768 * 100).astype(np.int16)    # +40 dB so int16 keeps detail
    a, _ = vc.clean_voice(as_int, SR)
    b, _ = vc.clean_voice(as_int.astype(np.float64) / 32768, SR)
    assert np.allclose(a, b, atol=1e-5)


def test_stereo_with_one_empty_channel_uses_the_live_one():
    _, noise, voice = _sample()
    mono = noise + voice
    stereo = np.stack([np.zeros_like(mono), mono], axis=1)
    a, _ = vc.clean_voice(stereo, SR)
    b, _ = vc.clean_voice(mono, SR)
    assert np.allclose(a, b, atol=1e-6)


def test_chunk_size_does_not_change_the_result():
    _, noise, voice = _sample()
    x = noise + voice
    assert np.allclose(vc.denoise(x, SR, chunk_frames=37), vc.denoise(x, SR, chunk_frames=4096),
                       atol=1e-12)


def test_leading_digital_silence_is_not_taken_as_noise():
    t, noise, voice = _sample()
    x = noise + voice
    x[:SR // 2] = 0.0                     # stream start delivered as zeros
    y = vc.denoise(x, SR)
    sil = t >= 3.3
    assert _db(noise[sil]) - _db(y[sil]) >= 12


def test_stream_start_pop_does_not_set_the_gain():
    t, noise, voice = _sample()
    x = noise + voice
    x[:2400] += 10 ** (-40 / 20) * np.sin(2 * np.pi * 110 * t[:2400])   # 50 ms pop, louder than the voice
    with_pop, info_pop = vc.clean_voice(x, SR)
    _, info_clean = vc.clean_voice(noise + voice, SR)
    assert abs(info_pop["gain_db"] - info_clean["gain_db"]) < 1.0
    assert np.max(np.abs(with_pop[:2400])) < 0.5 * np.max(np.abs(with_pop))


@pytest.mark.parametrize("n", [0, 100, 3000])
def test_very_short_input_does_not_crash(n):
    out, _ = vc.clean_voice(np.zeros(n), SR)
    assert out.shape == (n,)


def test_all_zeros_stays_zeros():
    out, info = vc.clean_voice(np.zeros(SR), SR)
    assert not out.any() and info["gain_db"] == 0.0

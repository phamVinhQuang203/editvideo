# -*- coding: utf-8 -*-
"""Run Demucs with a WAV writer that works with static FFmpeg on Windows."""
import runpy
import wave

import numpy as np

import demucs.audio


def _save_without_torchcodec(path, tensor, sample_rate, encoding=None,
                             bits_per_sample=16, **_kwargs):
    audio = tensor.detach().cpu().numpy().T
    payload = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2")
    channels = 1 if payload.ndim == 1 else payload.shape[1]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(int(sample_rate))
        output.writeframes(payload.tobytes())


demucs.audio.ta.save = _save_without_torchcodec
runpy.run_module("demucs", run_name="__main__")

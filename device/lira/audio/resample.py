"""Convert generated float PCM to the realtime service's 16 kHz input."""
import numpy as np


def to_16khz(samples, source_rate):
    if source_rate == 16000:
        return np.asarray(samples,dtype=np.float32)
    try:
        from scipy.signal import resample_poly
        from math import gcd
        divisor=gcd(source_rate,16000)
        return resample_poly(samples,16000//divisor,source_rate//divisor).astype(np.float32)
    except ImportError:
        grid=np.arange(127)-63
        cutoff=.45*16000/source_rate
        kernel=2*cutoff*np.sinc(2*cutoff*grid)*np.hamming(127)
        filtered=np.convolve(samples,kernel/kernel.sum(),mode='same')
        positions=np.arange(round(len(samples)*16000/source_rate))*source_rate/16000
        return np.interp(positions,np.arange(len(samples)),filtered).astype(np.float32)

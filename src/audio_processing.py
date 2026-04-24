import numpy as np
from scipy.fftpack import fft
from scipy.signal import butter, lfilter

def sigmoid(x, factor = 25):
    return 1 / (1 + np.exp(-factor * (x - 0.5)))

def calculate_spectrum(data, n_bars, window, mean_range = 1, sample_rate=44100):
    windowed_data = data * window
    data_fft = fft(windowed_data)
    spectrum = np.abs(data_fft)[:len(data_fft)]
    
    mask = np.logspace(30, 1, (len(spectrum) // 6), True, 0.9)
    spectrum[:len(mask)] = spectrum[:len(mask)] * mask
    spectrum[-len(mask):] = spectrum[-len(mask):] * mask[::-1]

    spectrum = np.concatenate([spectrum[len(spectrum) // 2:], spectrum[:len(spectrum) // 2]])

    pivot1 = len(spectrum) // 2
    pivot2 = len(spectrum) // 6
    pivot3 = len(spectrum) // 6 * 5
    lens = len(spectrum)
    spectrum[:pivot2] = spectrum[pivot1 + 2 * pivot2:pivot1:-2] / 2
    spectrum[pivot3:] = spectrum[pivot1:pivot1 - 2 * (lens - pivot3):-2] / 2

    # Calculate the mean of the spectrum for each bar
    freq_per_bar = len(spectrum) // n_bars
    spectrum_bars = np.array([np.mean(spectrum[i * freq_per_bar: (i + mean_range) * freq_per_bar]) for i in range(n_bars)])

    return spectrum_bars    

def smooth_transition(prev, current):
    smoothing_factor = 0.7
    return prev * smoothing_factor + current * (1 - smoothing_factor)
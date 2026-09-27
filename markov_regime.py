import numpy as np
from typing import Optional

STATES = ['BULL', 'SIDEWAYS', 'BEAR']
STATE_IDX = {s: i for i, s in enumerate(STATES)}


def label_candles(closes: list, window: int = 20, threshold: float = 0.02) -> list:
    labels = []
    for i in range(len(closes)):
        if i < window:
            labels.append(None)
        else:
            rolling_return = (closes[i] - closes[i - window]) / closes[i - window]
            if rolling_return > threshold:
                labels.append('BULL')
            elif rolling_return < -threshold:
                labels.append('BEAR')
            else:
                labels.append('SIDEWAYS')
    return labels


def build_transition_matrix(labels: list) -> np.ndarray:
    P = np.zeros((3, 3))
    valid_labels = [l for l in labels if l is not None]
    
    for i in range(len(valid_labels) - 1):
        from_state = STATE_IDX[valid_labels[i]]
        to_state = STATE_IDX[valid_labels[i + 1]]
        P[from_state, to_state] += 1
    
    for i in range(3):
        row_sum = P[i].sum()
        if row_sum > 0:
            P[i] = P[i] / row_sum
        else:
            P[i] = np.array([1/3, 1/3, 1/3])
    
    return P


def stationary_distribution(P: np.ndarray) -> np.ndarray:
    try:
        eigenvalues, eigenvectors = np.linalg.eig(P.T)
        idx = np.argmin(np.abs(eigenvalues - 1.0))
        stat_dist = np.real(eigenvectors[:, idx])
        stat_dist = np.abs(stat_dist)
        stat_dist = stat_dist / stat_dist.sum()
        return stat_dist
    except Exception:
        return np.array([1/3, 1/3, 1/3])


def get_regime_signal(closes: list, window: int = 20, threshold: float = 0.02, min_train: int = 100) -> Optional[dict]:
    try:
        if len(closes) < min_train + window:
            return None
        
        labels = label_candles(closes, window, threshold)
        P = build_transition_matrix(labels)
        stat_dist = stationary_distribution(P)
        
        # Find last non-None label
        current_label = None
        for i in range(len(labels) - 1, -1, -1):
            if labels[i] is not None:
                current_label = labels[i]
                break
        
        if current_label is None:
            return None
        
        current_idx = STATE_IDX[current_label]
        row = P[current_idx]
        
        signal = round(float(row[0] - row[2]), 4)
        stay_prob = round(float(row[current_idx]), 4)
        bear_baseline = round(float(stat_dist[2]), 4)
        bull_baseline = round(float(stat_dist[0]), 4)
        
        # size_scalar: if bear <= 0.20: 1.0; if bear >= 0.60: 0.0; linear between
        if bear_baseline <= 0.20:
            size_scalar = 1.0
        elif bear_baseline >= 0.60:
            size_scalar = 0.0
        else:
            # Linear: at 0.20 -> 1.0, at 0.60 -> 0.0
            # scalar = 1.0 - (bear_baseline - 0.20) / 0.40 = (0.60 - bear_baseline) / 0.40
            size_scalar = (0.60 - bear_baseline) / 0.40
        
        size_scalar = round(max(0.0, min(1.0, size_scalar)), 4)
        
        return {
            'current_regime': current_label,
            'signal': signal,
            'stay_prob': stay_prob,
            'bear_baseline': bear_baseline,
            'bull_baseline': bull_baseline,
            'size_scalar': size_scalar,
        }
    except Exception:
        return None

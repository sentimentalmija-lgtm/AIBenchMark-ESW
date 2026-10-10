"""Descriptive independent-run statistics and labeled approximate intervals."""

import math
import statistics


def score_statistics(values):
    if not values:
        return {'mean': None, 'sample_stddev': None, 'mean_ci95_approx': None}
    mean = statistics.mean(values)
    deviation = statistics.stdev(values) if len(values) > 1 else None
    margin = 1.96 * deviation / math.sqrt(len(values)) if deviation is not None else None
    interval = ({'lower': round(max(0, mean - margin), 6), 'upper': round(min(100, mean + margin), 6),
                 'method': 'normal approximation; descriptive, small-sample uncertainty'} if margin is not None else None)
    return {'mean': round(mean, 6), 'sample_stddev': round(deviation, 6) if deviation is not None else None,
            'mean_ci95_approx': interval}


def interval_text(interval):
    return ('approximate 95% mean interval unavailable (one sample)' if interval is None else
            f"approximate 95% mean interval {interval['lower']:.2f}–{interval['upper']:.2f}; {interval['method']}")

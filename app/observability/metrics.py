"""
Prometheus-Compatible Metrics Collection
==========================================

Thread-safe metric collection with Counter, Histogram, and Gauge types.
When ``prometheus_client`` is installed, metrics are also registered with
the Prometheus registry for exposition.  Without it, metrics are collected
in-process and exposed via :meth:`get_metrics`.

Built-in metrics:
    - ``llm_calls_total`` (Counter)
    - ``llm_call_duration_seconds`` (Histogram)
    - ``tool_calls_total`` (Counter)
    - ``tool_call_duration_seconds`` (Histogram)
    - ``conversation_duration_seconds`` (Histogram)
    - ``active_conversations`` (Gauge)
    - ``token_usage_total`` (Counter)
    - ``error_count_total`` (Counter)

Usage::

    from app.observability.metrics import (
        llm_calls_total,
        llm_call_duration_seconds,
        inc_counter,
        observe_histogram,
        set_gauge,
        get_metrics,
    )

    # Increment a counter
    inc_counter("llm_calls_total", model="gpt-4o")

    # Time an operation
    with llm_call_duration_seconds.time():
        await call_llm(...)

    # Get all metrics as a dict
    all_metrics = get_metrics()
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Try importing prometheus_client — graceful degradation
# ---------------------------------------------------------------------------

_HAS_PROMETHEUS = False
_prom_reg = None

try:
    import prometheus_client as _prom
    from prometheus_client import (
        Counter as _PromCounter,
        Histogram as _PromHistogram,
        Gauge as _PromGauge,
        CollectorRegistry,
        generate_latest,
    )

    _HAS_PROMETHEUS = True
    _prom_reg = CollectorRegistry()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Internal metric storage (always available, even without prometheus_client)
# ---------------------------------------------------------------------------

_lock = threading.Lock()


class _LabelKey:
    """Hashable key for labeled metric entries."""

    __slots__ = ("_labels",)

    def __init__(self, labels: Optional[Dict[str, str]] = None) -> None:
        self._labels = tuple(sorted((labels or {}).items())) if labels else ()

    def __hash__(self) -> int:
        return hash(self._labels)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _LabelKey):
            return NotImplemented
        return self._labels == other._labels


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------

class Counter:
    """A monotonically increasing counter.

    Thread-safe.  Optionally backed by ``prometheus_client.Counter``.
    """

    def __init__(self, name: str, description: str = "",
                 label_names: Optional[List[str]] = None) -> None:
        self.name = name
        self.description = description
        self.label_names = label_names or []
        self._values: Dict[_LabelKey, float] = {}
        self._lock = threading.Lock()
        self._prom_counter: Optional[Any] = None

        if _HAS_PROMETHEUS:
            try:
                self._prom_counter = _PromCounter(
                    name, description,
                    labelnames=self.label_names,
                    registry=_prom_reg,
                )
            except Exception:
                pass

    def inc(self, amount: float = 1.0,
            labels: Optional[Dict[str, str]] = None) -> None:
        """Increment the counter by ``amount``.

        Args:
            amount: Value to add. Must be non-negative.
            labels: Optional label key-value pairs.
        """
        if amount < 0:
            raise ValueError("Counter increment must be non-negative")

        key = _LabelKey(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

        if self._prom_counter is not None:
            try:
                if labels:
                    self._prom_counter.labels(**labels).inc(amount)
                else:
                    self._prom_counter.inc(amount)
            except Exception:
                pass

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """Return the current counter value for the given labels."""
        key = _LabelKey(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def get_all(self) -> Dict[str, float]:
        """Return all labeled values as ``{"label_str": value}``."""
        result: Dict[str, float] = {}
        with self._lock:
            for key, value in self._values.items():
                label_str = ",".join(f"{k}={v}" for k, v in key._labels)
                result[label_str or "total"] = value
        return result


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------

# Default bucket boundaries (seconds)
_DEFAULT_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0,
    2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)


class Histogram:
    """A histogram that tracks value distribution across buckets.

    Thread-safe.  Optionally backed by ``prometheus_client.Histogram``.
    """

    def __init__(self, name: str, description: str = "",
                 label_names: Optional[List[str]] = None,
                 buckets: Optional[Sequence[float]] = None) -> None:
        self.name = name
        self.description = description
        self.label_names = label_names or []
        self._buckets = tuple(sorted(buckets or _DEFAULT_BUCKETS))
        self._bucket_counts: Dict[_LabelKey, Dict[float, int]] = {}
        self._sums: Dict[_LabelKey, float] = {}
        self._counts: Dict[_LabelKey, int] = {}
        self._lock = threading.Lock()
        self._prom_histogram: Optional[Any] = None

        if _HAS_PROMETHEUS:
            try:
                self._prom_histogram = _PromHistogram(
                    name, description,
                    labelnames=self.label_names,
                    buckets=self._buckets,
                    registry=_prom_reg,
                )
            except Exception:
                pass

    def observe(self, value: float,
                labels: Optional[Dict[str, str]] = None) -> None:
        """Record an observation.

        Args:
            value: The observed value.
            labels: Optional label key-value pairs.
        """
        key = _LabelKey(labels)
        with self._lock:
            # Update sum and count
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._counts[key] = self._counts.get(key, 0) + 1

            # Update bucket counts
            if key not in self._bucket_counts:
                self._bucket_counts[key] = {b: 0 for b in self._buckets}
            buckets = self._bucket_counts[key]
            for boundary in self._buckets:
                if value <= boundary:
                    buckets[boundary] += 1

        if self._prom_histogram is not None:
            try:
                if labels:
                    self._prom_histogram.labels(**labels).observe(value)
                else:
                    self._prom_histogram.observe(value)
            except Exception:
                pass

    def time(self, labels: Optional[Dict[str, str]] = None) -> "_HistogramTimer":
        """Return a context manager that times a block and records it.

        Usage::

            with histogram.time(labels={"model": "gpt-4o"}):
                await call_llm(...)
        """
        return _HistogramTimer(self, labels)

    def get(self, labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Return histogram stats for the given labels."""
        key = _LabelKey(labels)
        with self._lock:
            count = self._counts.get(key, 0)
            total = self._sums.get(key, 0.0)
            buckets = self._bucket_counts.get(key, {})
            return {
                "count": count,
                "sum": total,
                "avg": (total / count) if count > 0 else 0.0,
                "buckets": {str(b): c for b, c in buckets.items()},
            }

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Return histogram stats for all label combinations."""
        result: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for key in self._counts:
                label_str = ",".join(f"{k}={v}" for k, v in key._labels)
                result[label_str or "total"] = self.get(
                    dict(key._labels) if key._labels else None
                )
        return result


class _HistogramTimer:
    """Context manager for timing histogram observations."""

    __slots__ = ("_histogram", "_labels", "_start")

    def __init__(self, histogram: Histogram,
                 labels: Optional[Dict[str, str]] = None) -> None:
        self._histogram = histogram
        self._labels = labels
        self._start: float = 0.0

    def __enter__(self) -> "_HistogramTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        duration = time.monotonic() - self._start
        self._histogram.observe(duration, labels=self._labels)


# ---------------------------------------------------------------------------
# Gauge
# ---------------------------------------------------------------------------

class Gauge:
    """A gauge that can go up and down.

    Thread-safe.  Optionally backed by ``prometheus_client.Gauge``.
    """

    def __init__(self, name: str, description: str = "",
                 label_names: Optional[List[str]] = None) -> None:
        self.name = name
        self.description = description
        self.label_names = label_names or []
        self._values: Dict[_LabelKey, float] = {}
        self._lock = threading.Lock()
        self._prom_gauge: Optional[Any] = None

        if _HAS_PROMETHEUS:
            try:
                self._prom_gauge = _PromGauge(
                    name, description,
                    labelnames=self.label_names,
                    registry=_prom_reg,
                )
            except Exception:
                pass

    def set(self, value: float,
            labels: Optional[Dict[str, str]] = None) -> None:
        """Set the gauge to an arbitrary value.

        Args:
            value: The new gauge value.
            labels: Optional label key-value pairs.
        """
        key = _LabelKey(labels)
        with self._lock:
            self._values[key] = value

        if self._prom_gauge is not None:
            try:
                if labels:
                    self._prom_gauge.labels(**labels).set(value)
                else:
                    self._prom_gauge.set(value)
            except Exception:
                pass

    def inc(self, amount: float = 1.0,
            labels: Optional[Dict[str, str]] = None) -> None:
        """Increment the gauge by ``amount``."""
        key = _LabelKey(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

        if self._prom_gauge is not None:
            try:
                if labels:
                    self._prom_gauge.labels(**labels).inc(amount)
                else:
                    self._prom_gauge.inc(amount)
            except Exception:
                pass

    def dec(self, amount: float = 1.0,
            labels: Optional[Dict[str, str]] = None) -> None:
        """Decrement the gauge by ``amount``."""
        self.inc(-amount, labels=labels)

    def get(self, labels: Optional[Dict[str, str]] = None) -> float:
        """Return the current gauge value for the given labels."""
        key = _LabelKey(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def get_all(self) -> Dict[str, float]:
        """Return all labeled values."""
        result: Dict[str, float] = {}
        with self._lock:
            for key, value in self._values.items():
                label_str = ",".join(f"{k}={v}" for k, v in key._labels)
                result[label_str or "value"] = value
        return result


# ---------------------------------------------------------------------------
# Built-in metrics
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "llm_calls_total",
    "Total number of LLM API calls",
    label_names=["model", "provider", "status"],
)

llm_call_duration_seconds = Histogram(
    "llm_call_duration_seconds",
    "Duration of LLM API calls in seconds",
    label_names=["model", "provider"],
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

tool_calls_total = Counter(
    "tool_calls_total",
    "Total number of tool executions",
    label_names=["tool_name", "status"],
)

tool_call_duration_seconds = Histogram(
    "tool_call_duration_seconds",
    "Duration of tool executions in seconds",
    label_names=["tool_name"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0),
)

conversation_duration_seconds = Histogram(
    "conversation_duration_seconds",
    "Duration of conversations in seconds",
    label_names=["agent_name", "mode"],
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0, 3600.0),
)

active_conversations = Gauge(
    "active_conversations",
    "Number of currently active conversations",
    label_names=["agent_name"],
)

token_usage_total = Counter(
    "token_usage_total",
    "Total token usage across LLM calls",
    label_names=["model", "token_type"],
)

error_count_total = Counter(
    "error_count_total",
    "Total number of errors",
    label_names=["component", "error_type"],
)

# Registry of all built-in metrics for easy iteration
_BUILTIN_METRICS: Dict[str, Union[Counter, Histogram, Gauge]] = {
    "llm_calls_total": llm_calls_total,
    "llm_call_duration_seconds": llm_call_duration_seconds,
    "tool_calls_total": tool_calls_total,
    "tool_call_duration_seconds": tool_call_duration_seconds,
    "conversation_duration_seconds": conversation_duration_seconds,
    "active_conversations": active_conversations,
    "token_usage_total": token_usage_total,
    "error_count_total": error_count_total,
}

# Custom metrics registry
_CUSTOM_METRICS: Dict[str, Union[Counter, Histogram, Gauge]] = {}


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def inc_counter(name: str, amount: float = 1.0,
                labels: Optional[Dict[str, str]] = None) -> None:
    """Increment a counter by name.

    Looks up built-in metrics first, then custom metrics.

    Args:
        name: Metric name.
        amount: Amount to increment.
        labels: Optional label key-value pairs.

    Raises:
        KeyError: If the metric name is not found.
    """
    metric = _BUILTIN_METRICS.get(name) or _CUSTOM_METRICS.get(name)
    if metric is None:
        raise KeyError(f"Counter '{name}' not found")
    if not isinstance(metric, Counter):
        raise TypeError(f"Metric '{name}' is not a Counter (got {type(metric).__name__})")
    metric.inc(amount, labels=labels)


def observe_histogram(name: str, value: float,
                      labels: Optional[Dict[str, str]] = None) -> None:
    """Record a histogram observation by name.

    Args:
        name: Metric name.
        value: The observed value.
        labels: Optional label key-value pairs.

    Raises:
        KeyError: If the metric name is not found.
    """
    metric = _BUILTIN_METRICS.get(name) or _CUSTOM_METRICS.get(name)
    if metric is None:
        raise KeyError(f"Histogram '{name}' not found")
    if not isinstance(metric, Histogram):
        raise TypeError(f"Metric '{name}' is not a Histogram (got {type(metric).__name__})")
    metric.observe(value, labels=labels)


def set_gauge(name: str, value: float,
              labels: Optional[Dict[str, str]] = None) -> None:
    """Set a gauge value by name.

    Args:
        name: Metric name.
        value: The new gauge value.
        labels: Optional label key-value pairs.

    Raises:
        KeyError: If the metric name is not found.
    """
    metric = _BUILTIN_METRICS.get(name) or _CUSTOM_METRICS.get(name)
    if metric is None:
        raise KeyError(f"Gauge '{name}' not found")
    if not isinstance(metric, Gauge):
        raise TypeError(f"Metric '{name}' is not a Gauge (got {type(metric).__name__})")
    metric.set(value, labels=labels)


def register_counter(name: str, description: str = "",
                     label_names: Optional[List[str]] = None) -> Counter:
    """Register a custom counter metric.

    Args:
        name: Metric name (must be unique).
        description: Human-readable description.
        label_names: Optional list of label names.

    Returns:
        The new Counter instance.

    Raises:
        ValueError: If a metric with the same name already exists.
    """
    with _lock:
        if name in _BUILTIN_METRICS or name in _CUSTOM_METRICS:
            raise ValueError(f"Metric '{name}' already registered")
        counter = Counter(name, description, label_names=label_names)
        _CUSTOM_METRICS[name] = counter
        return counter


def register_histogram(name: str, description: str = "",
                       label_names: Optional[List[str]] = None,
                       buckets: Optional[Sequence[float]] = None) -> Histogram:
    """Register a custom histogram metric.

    Args:
        name: Metric name (must be unique).
        description: Human-readable description.
        label_names: Optional list of label names.
        buckets: Optional bucket boundaries.

    Returns:
        The new Histogram instance.

    Raises:
        ValueError: If a metric with the same name already exists.
    """
    with _lock:
        if name in _BUILTIN_METRICS or name in _CUSTOM_METRICS:
            raise ValueError(f"Metric '{name}' already registered")
        hist = Histogram(name, description, label_names=label_names, buckets=buckets)
        _CUSTOM_METRICS[name] = hist
        return hist


def register_gauge(name: str, description: str = "",
                   label_names: Optional[List[str]] = None) -> Gauge:
    """Register a custom gauge metric.

    Args:
        name: Metric name (must be unique).
        description: Human-readable description.
        label_names: Optional list of label names.

    Returns:
        The new Gauge instance.

    Raises:
        ValueError: If a metric with the same name already exists.
    """
    with _lock:
        if name in _BUILTIN_METRICS or name in _CUSTOM_METRICS:
            raise ValueError(f"Metric '{name}' already registered")
        gauge = Gauge(name, description, label_names=label_names)
        _CUSTOM_METRICS[name] = gauge
        return gauge


# ---------------------------------------------------------------------------
# Metrics collection and exposition
# ---------------------------------------------------------------------------

def get_metrics() -> Dict[str, Any]:
    """Return all metrics as a dict suitable for JSON serialization.

    Returns:
        A dict with metric names as keys and their values/stats.
    """
    result: Dict[str, Any] = {}

    all_metrics: Dict[str, Union[Counter, Histogram, Gauge]] = {}
    all_metrics.update(_BUILTIN_METRICS)
    all_metrics.update(_CUSTOM_METRICS)

    for name, metric in all_metrics.items():
        if isinstance(metric, Counter):
            result[name] = {
                "type": "counter",
                "description": metric.description,
                "values": metric.get_all(),
            }
        elif isinstance(metric, Histogram):
            result[name] = {
                "type": "histogram",
                "description": metric.description,
                "values": metric.get_all(),
            }
        elif isinstance(metric, Gauge):
            result[name] = {
                "type": "gauge",
                "description": metric.description,
                "values": metric.get_all(),
            }

    return result


def generate_prometheus_output() -> str:
    """Generate Prometheus-compatible exposition text.

    Returns:
        A string in Prometheus exposition format, or an empty string if
        ``prometheus_client`` is not installed.
    """
    if not _HAS_PROMETHEUS or _prom_reg is None:
        return ""
    try:
        return generate_latest(_prom_reg).decode("utf-8")
    except Exception:
        return ""


def is_prometheus_available() -> bool:
    """Return ``True`` if ``prometheus_client`` is installed."""
    return _HAS_PROMETHEUS

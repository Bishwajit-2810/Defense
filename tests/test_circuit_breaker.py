"""Unit tests for libs/llm/circuit.py — LLM backend circuit breaker (§8)."""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

from libs.llm.circuit import CircuitBreaker


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_opens_after_threshold():
    clk = FakeClock()
    cb = CircuitBreaker("groq", failure_threshold=3, cooldown_seconds=30, clock=clk)
    assert cb.state == "closed" and cb.allow()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"          # below threshold
    cb.record_failure()
    assert cb.state == "open" and not cb.allow()


def test_half_open_after_cooldown_then_close_on_success():
    clk = FakeClock()
    cb = CircuitBreaker("groq", failure_threshold=1, cooldown_seconds=30, clock=clk)
    cb.record_failure()
    assert cb.state == "open"
    clk.advance(31)
    assert cb.state == "half_open" and cb.allow()
    cb.record_success()
    assert cb.state == "closed"


def test_half_open_failure_reopens():
    clk = FakeClock()
    cb = CircuitBreaker("groq", failure_threshold=1, cooldown_seconds=30, clock=clk)
    cb.record_failure()
    clk.advance(31)
    assert cb.state == "half_open"
    cb.record_failure()                  # trial failed
    assert cb.state == "open"


def test_success_resets_failure_count():
    clk = FakeClock()
    cb = CircuitBreaker("local", failure_threshold=3, cooldown_seconds=30, clock=clk)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()                  # resets
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "closed"          # only 2 consecutive since reset

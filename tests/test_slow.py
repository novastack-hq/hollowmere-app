"""Release 1.0.1: the SLOW_MS setting (lab FEST-46, the headliner reveal)."""

import time

import app as fest


def client():
    fest.app.config["TESTING"] = True
    return fest.app.test_client()


def took(path):
    started = time.perf_counter()
    reply = client().get(path)
    assert reply.status_code == 200
    return time.perf_counter() - started


def test_slow_ms_is_off_by_default():
    assert fest.SLOW_MS == 0
    assert took("/lineup") < 0.2


def test_slow_ms_delays_the_lineup(monkeypatch):
    monkeypatch.setattr(fest, "SLOW_MS", 300)
    assert took("/lineup?ticket=102") >= 0.3


def test_slow_ms_leaves_health_fast(monkeypatch):
    monkeypatch.setattr(fest, "SLOW_MS", 300)
    assert took("/health") < 0.2

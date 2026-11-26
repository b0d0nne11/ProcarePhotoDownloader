from unittest.mock import patch

import pytest

from procare_photo_downloader.ratelimit import TokenBucket


@pytest.fixture(autouse=True)
def mock_monotonic():
    with patch("time.monotonic", return_value=0.0) as monotonic:
        yield monotonic


@pytest.fixture(autouse=True)
def mock_sleep(mock_monotonic):
    def advance(seconds):
        mock_monotonic.return_value += seconds
    with patch("time.sleep", side_effect=advance) as sleep:
        yield sleep


class TestTokenBucket:

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            pytest.param(
                {"rate": 0},
                "rate must be positive",
                id="rate",
            ),
            pytest.param(
                {"rate": 10, "capacity": 5},
                "capacity must be greater than or equal to rate",
                id="capacity",
            ),
            pytest.param(
                {"rate": 10, "min_rate": 0},
                "min_rate must be greater than 0 and less than or equal to rate",
                id="min_rate_too_low",
            ),
            pytest.param(
                {"rate": 10, "min_rate": 11},
                "min_rate must be greater than 0 and less than or equal to rate",
                id="min_rate_too_high",
            ),
            pytest.param(
                {"rate": 10, "backoff_factor": 1},
                "backoff_factor must be between 0 and 1",
                id="backoff_factor",
            ),
            pytest.param(
                {"rate": 10, "recovery_step_factor": 0},
                "recovery_step_factor must be between 0 and 1",
                id="recovery_step_factor",
            ),
        ],
    )
    def test_invalid_arguments(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            TokenBucket(**kwargs)

    def test_rate(self):
        bucket = TokenBucket(rate=10)
        assert bucket.rate == 10

    def test_acquire(self, mock_sleep):
        bucket = TokenBucket(rate=10)
        for _ in range(10):
            bucket.acquire()
        mock_sleep.assert_not_called()

    def test_acquire_bursts_to_capacity(self, mock_sleep):
        bucket = TokenBucket(rate=10, capacity=20)
        for _ in range(20):
            bucket.acquire()
        mock_sleep.assert_not_called()

    def test_acquire_waits_for_tokens(self, mock_sleep):
        bucket = TokenBucket(rate=10)
        bucket.acquire(10)
        bucket.acquire()
        mock_sleep.assert_called_once_with(0.1)

    def test_acquire_refills_over_time(self, mock_sleep, mock_monotonic):
        bucket = TokenBucket(rate=10)
        bucket.acquire(10)
        mock_monotonic.return_value += 0.5
        bucket.acquire(5)
        mock_sleep.assert_not_called()

    def test_backoff(self, mock_sleep):
        bucket = TokenBucket(rate=10)
        bucket.backoff()
        assert bucket.rate == 6
        bucket.acquire()
        mock_sleep.assert_called_once_with(1 / 6)

    def test_recover(self):
        bucket = TokenBucket(rate=10)
        bucket.backoff()
        bucket.recover()
        assert bucket.rate == 6.5

    def test_backoff_min_rate(self):
        bucket = TokenBucket(rate=10)
        for _ in range(10):
            bucket.backoff()
        assert bucket.rate == 1

    def test_recover_max_rate(self):
        bucket = TokenBucket(rate=10)
        bucket.recover()
        assert bucket.rate == 10

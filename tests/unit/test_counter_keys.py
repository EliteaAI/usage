"""Counter grain: period truncation and the two sentinel conventions."""
from datetime import date, datetime, timedelta, timezone

import pytest

from usage.methods import _counters as counters


class TestPeriodStart:
    def test_mid_month_truncates_to_the_first(self):
        assert counters.period_start(datetime(2026, 9, 17, 13, 45, tzinfo=timezone.utc)) \
            == date(2026, 9, 1)

    def test_returns_a_date_not_a_datetime(self):
        # The PK column is DATE; a datetime would never compare equal on lookup.
        result = counters.period_start(datetime(2026, 9, 17, tzinfo=timezone.utc))
        #
        assert type(result) is date  # noqa: E721 - a datetime is a date subclass

    def test_accepts_a_plain_date(self):
        assert counters.period_start(date(2026, 9, 17)) == date(2026, 9, 1)

    def test_last_instant_of_the_month_stays_in_that_month(self):
        moment = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
        #
        assert counters.period_start(moment) == date(2026, 9, 1)

    def test_utc_boundary_uses_utc_not_local_time(self):
        # 2026-10-01T00:00Z is October even where local time still says September.
        assert counters.period_start(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)) \
            == date(2026, 10, 1)

    def test_non_utc_timestamp_is_normalised_to_utc(self):
        moment = datetime(2026, 10, 1, 1, 0, tzinfo=timezone(timedelta(hours=3)))
        #
        assert counters.period_start(moment) == date(2026, 9, 1)

    def test_unsupported_period_kind_raises(self):
        with pytest.raises(ValueError):
            counters.period_start(date(2026, 9, 17), period_kind="week")

    def test_wrong_type_raises(self):
        with pytest.raises(TypeError):
            counters.period_start("2026-09-17")


class TestKeys:
    def test_project_key_uses_both_sentinels(self):
        key = counters.project_key(7, date(2026, 9, 17))
        #
        assert key["project_id"] == 7
        assert key["user_id"] == counters.PROJECT_USER_SENTINEL == 0
        assert key["model_name"] == counters.ALL_MODELS_SENTINEL == ""
        assert key["period_kind"] == counters.PERIOD_MONTH
        assert key["period_start"] == date(2026, 9, 1)

    def test_member_key_keeps_the_real_user_id(self):
        key = counters.member_key(7, 42, date(2026, 9, 17))
        #
        assert key["user_id"] == 42
        assert key["model_name"] == ""

    def test_member_key_rejects_the_reserved_user_id(self):
        # Writing user_id=0 as a member row would silently corrupt the project total.
        with pytest.raises(ValueError):
            counters.member_key(7, 0, date(2026, 9, 17))

    def test_per_model_keys_carry_the_model(self):
        key = counters.project_key(7, date(2026, 9, 17), model_name="gpt-4o")
        #
        assert key["model_name"] == "gpt-4o"

    def test_keys_have_exactly_the_primary_key_columns(self):
        expected = {"project_id", "user_id", "period_kind", "period_start", "model_name"}
        #
        assert set(counters.project_key(7, date(2026, 9, 17))) == expected
        assert set(counters.member_key(7, 42, date(2026, 9, 17))) == expected

    def test_none_model_name_is_the_all_models_sentinel_not_null(self):
        # model_name is NOT NULL in the PK, so None must become ''.
        assert counters.project_key(7, date(2026, 9, 17), model_name=None)["model_name"] == ""

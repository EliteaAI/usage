"""below_resolution_sums()/below_resolution() flag costs that were priced but too small to store (#6682).

Costs are stored as integer micro-USD, so a priced call under $0.0000005 (e.g. a 7-token
text-embedding-3-small query) is written as 0. The dashboard must tell that apart from a truly
free call and from an unpriced model — both of which also store 0. The real SQL expression runs
against an in-memory table so a regression in the CASE predicate fails here.
"""
from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import Session

from usage.methods import _analytics

METADATA = MetaData()

FAKE_TABLE = Table(
    "usage_event", METADATA,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("cost_source", String),
    Column("cost_micro_usd", BigInteger, default=0),
    Column("input_cost_micro_usd", BigInteger, default=0),
    Column("output_cost_micro_usd", BigInteger, default=0),
    Column("cache_read_cost_micro_usd", BigInteger, default=0),
    Column("cache_creation_cost_micro_usd", BigInteger, default=0),
    Column("billable_input_tokens", BigInteger, default=0),
    Column("output_tokens", BigInteger, default=0),
    Column("cache_read_tokens", BigInteger, default=0),
    Column("cache_creation_tokens", BigInteger, default=0),
)


class FakeUsageEvent:
    """Same attribute names as the ORM class, backed by FAKE_TABLE."""


for _column in FAKE_TABLE.c:
    setattr(FakeUsageEvent, _column.name, _column)


class TestBelowResolution:
    def setup_method(self, _method):
        self._patch = _analytics.UsageEvent
        _analytics.UsageEvent = FakeUsageEvent
        self.engine = create_engine("sqlite://")
        METADATA.create_all(self.engine)
        self.session = Session(self.engine)

    def teardown_method(self, _method):
        _analytics.UsageEvent = self._patch
        self.session.close()
        METADATA.drop_all(self.engine)

    def _insert(self, **values):
        self.session.execute(FAKE_TABLE.insert().values(**values))
        self.session.commit()

    def _flags(self, prefix=""):
        row = self.session.execute(select(*_analytics.below_resolution_sums())).mappings().one()
        return _analytics.below_resolution(dict(row), prefix=prefix)

    def test_tiny_priced_embedding_is_flagged_on_total_and_input(self):
        # The #6682 case: 7 input tokens at $2e-8 = $1.4e-7, stored as 0 micro-USD.
        self._insert(cost_source="estimated:costs-catalog", billable_input_tokens=7)
        assert self._flags() == {"total_cost": True, "input_cost": True}

    def test_kpi_prefix_applies_to_components_not_total(self):
        self._insert(cost_source="estimated:costs-catalog", billable_input_tokens=7)
        assert self._flags(prefix="total_") == {"total_cost": True, "total_input_cost": True}

    def test_unpriced_model_is_not_flagged(self):
        # Unpriced also stores 0 with tokens > 0; showing "< $0.00001" there would be a lie.
        self._insert(cost_source="unpriced", billable_input_tokens=5000, output_tokens=200)
        assert self._flags() == {}

    def test_null_cost_source_is_treated_as_unpriced(self):
        self._insert(cost_source=None, billable_input_tokens=12)
        assert self._flags() == {}

    def test_priced_call_with_stored_cost_is_not_flagged(self):
        self._insert(cost_source="estimated:costs-catalog", billable_input_tokens=1000,
                     cost_micro_usd=20, input_cost_micro_usd=20)
        assert self._flags() == {}

    def test_component_without_tokens_is_not_flagged(self):
        # Embedding: no output tokens, so a 0 output cost is genuinely zero.
        self._insert(cost_source="estimated:costs-catalog", billable_input_tokens=7)
        assert "output_cost" not in self._flags()

    def test_tiny_component_on_a_normal_call_flags_only_that_component(self):
        self._insert(cost_source="estimated:costs-catalog", billable_input_tokens=1000,
                     cache_creation_tokens=3, cost_micro_usd=20, input_cost_micro_usd=20)
        assert self._flags() == {"cache_creation_cost": True}

    def test_zero_token_row_is_not_flagged(self):
        self._insert(cost_source="estimated:costs-catalog")
        assert self._flags() == {}

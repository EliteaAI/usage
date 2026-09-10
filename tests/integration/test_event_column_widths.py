"""Every label the hooks can write must fit the column it lands in.

A too-narrow column does not degrade gracefully: the insert raises
StringDataRightTruncation and the whole call vanishes from billing, which is the exact
defect this feature exists to remove. The costs catalog's own tag
('estimated:costs-catalog', 23 chars) overflowed a varchar(16) in a live run, so these
widths are asserted against the real vocabularies rather than eyeballed.
"""
from usage import hooks
from usage.models.usage_event import UsageEvent
from usage.sources import base, registry


CATALOG_COST_SOURCES = ("estimated:costs-catalog", hooks.COST_SOURCE_UNPRICED)


def width(column_name):
    return UsageEvent.__table__.columns[column_name].type.length


def test_cost_source_fits_every_tag_the_catalog_returns():
    assert max(len(tag) for tag in CATALOG_COST_SOURCES) <= width("cost_source")


def test_token_source_fits_every_value():
    values = (
        base.TOKEN_SOURCE_PROVIDER, base.TOKEN_SOURCE_ESTIMATED, base.TOKEN_SOURCE_UNPARSED,
    )
    #
    assert max(len(value) for value in values) <= width("token_source")


def test_dialect_fits_every_registered_id():
    registry.clear()
    registry.register_defaults()
    try:
        assert max(len(name) for name in registry.all()) <= width("dialect")
    finally:
        registry.clear()

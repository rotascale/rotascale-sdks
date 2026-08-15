"""The OTel annotation helper (`#204`).

subhadipmitra@: The double is three lines because that is the point — nothing in
`rotascale.otel` imports opentelemetry, so nothing here needs it either. If this
file ever needs the real SDK to test, the module has grown a dependency it was
built to avoid.
"""

from rotascale.otel import (
    AGENT,
    DISCHARGES,
    GRANT,
    SOURCE_REF,
    STEP_KIND,
    TAINTING,
    TOOL_CALL,
    TRUSTED,
    govern,
    resource_attributes,
)


class FakeSpan:
    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value


def test_it_sets_only_what_it_is_given():
    span = govern(FakeSpan(), agent="refund-bot", kind=TOOL_CALL)
    assert span.attributes == {AGENT: "refund-bot", STEP_KIND: TOOL_CALL}


def test_a_second_call_does_not_clear_the_first():
    """Called once at span start and again when the tool name is known."""
    span = FakeSpan()
    govern(span, agent="refund-bot", kind=TOOL_CALL)
    govern(span, source_ref="invoices-api", grant="grt_123")
    assert span.attributes[AGENT] == "refund-bot"
    assert span.attributes[STEP_KIND] == TOOL_CALL
    assert span.attributes[SOURCE_REF] == "invoices-api"
    assert span.attributes[GRANT] == "grt_123"


def test_false_is_recorded_and_none_is_not():
    """`trusted=False` is a statement; `trusted=None` is silence. Collapsing
    them would let a caller who said nothing appear to have attested nothing
    is trusted, or vice versa."""
    assert govern(FakeSpan(), trusted=False).attributes == {TRUSTED: False}
    assert govern(FakeSpan(), trusted=None).attributes == {}


def test_discharges_accepts_a_list_or_a_string():
    assert govern(FakeSpan(), discharges=["untrusted_web", "third_party_tool"]
                  ).attributes[DISCHARGES] == "untrusted_web,third_party_tool"
    assert govern(FakeSpan(), discharges="untrusted_web"
                  ).attributes[DISCHARGES] == "untrusted_web"


def test_the_span_comes_back_for_chaining():
    span = FakeSpan()
    assert govern(span, agent="a") is span


def test_resource_attributes_name_the_agent():
    assert resource_attributes("refund-bot") == {AGENT: "refund-bot"}


def test_the_tainting_kinds_are_the_ones_the_receiver_never_infers():
    """Pinned deliberately. If a kind is added to TAINTING here without the
    receiver's inference table agreeing, one side taints and the other does
    not, and the disagreement shows up as a gate that fires inconsistently."""
    assert TAINTING == {"tool_call", "retrieval", "delegation"}

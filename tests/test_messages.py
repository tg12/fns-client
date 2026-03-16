"""Tests for XML message parsing."""

from __future__ import annotations

from fns_client.messages import FnsMessage, NotamStatus

SAMPLE_AIXM = """
<AIXMBasicMessage xmlns:gml="http://www.opengis.net/gml/3.2" gml:id="AIXM_BASIC_1001">
  <hasMember>
    <Event>
      <timeSlice>
        <EventTimeSlice>
          <validTime>
            <TimePeriod>
              <beginPosition>2026-03-15T12:00:00Z</beginPosition>
              <endPosition>2026-03-15T18:00:00Z</endPosition>
            </TimePeriod>
          </validTime>
          <textNOTAM>
            <NOTAM>
              <issued>2026-03-15T11:55:00Z</issued>
              <location>KATL</location>
              <text>RWY 09/27 CLOSED</text>
              <effectiveEnd>2026-03-15T18:00:00Z</effectiveEnd>
            </NOTAM>
          </textNOTAM>
          <extension>
            <EventExtension>
              <lastUpdated>2026-03-15T11:56:00Z</lastUpdated>
              <classification>DOM</classification>
              <accountId>KZTL</accountId>
            </EventExtension>
          </extension>
        </EventTimeSlice>
      </timeSlice>
    </Event>
  </hasMember>
</AIXMBasicMessage>
"""


def test_fns_message_from_xml_extracts_fields() -> None:
    message = FnsMessage.from_xml(77, SAMPLE_AIXM, status=NotamStatus.ACTIVE)

    assert message.fns_id == 1001
    assert message.correlation_id == 77
    assert message.location_designator == "KATL"
    assert message.classification == "DOM"
    assert message.notam_accountability == "KZTL"
    assert message.status == NotamStatus.ACTIVE

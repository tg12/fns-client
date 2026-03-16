"""Tests for service bootstrap behavior."""

from __future__ import annotations

from pathlib import Path

from fns_client.config import AppConfig, DatabaseConfig, JmsConfig, RestApiConfig
from fns_client.service import FnsClientService

SAMPLE_AIXM = """
<AIXMBasicMessage xmlns:gml="http://www.opengis.net/gml/3.2" gml:id="AIXM_BASIC_1001">
  <hasMember>
    <Event>
      <timeSlice>
        <EventTimeSlice>
          <validTime>
            <TimePeriod>
              <beginPosition>2026-03-15T12:00:00Z</beginPosition>
              <endPosition>2036-03-15T18:00:00Z</endPosition>
            </TimePeriod>
          </validTime>
          <textNOTAM>
            <NOTAM>
              <issued>2026-03-15T11:55:00Z</issued>
              <location>KATL</location>
              <text>RWY 09/27 CLOSED</text>
              <effectiveEnd>2036-03-15T18:00:00Z</effectiveEnd>
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


def test_initialize_notam_db_from_replay(tmp_path: Path) -> None:
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "sample.xml").write_text(SAMPLE_AIXM, encoding="utf-8")

    service = FnsClientService(
        AppConfig(
            database=DatabaseConfig(
                connection_url=f"sqlite:///{tmp_path / 'notams.sqlite3'}"
            ),
            jms=JmsConfig(enabled=False),
            rest_api=RestApiConfig(enabled=False),
        ),
        replay_path=replay_dir,
    )

    service.initialize_notam_db()

    assert service.validate_notam_db() is True

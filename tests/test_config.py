"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

from fns_client.config import load_config, parse_typesafe_style_config


def test_parse_typesafe_style_config_handles_comments_and_nested_keys() -> None:
    content = """
    jms.providerUrl="tcps://broker.example.com:55443" # inline comment
    restapi.enabled=true
    notamDb.connectionUrl="jdbc:h2:./Notams;mode=MySQL;AUTO_SERVER=TRUE"
    """
    parsed = parse_typesafe_style_config(content)

    assert parsed["jms"]["providerUrl"] == "tcps://broker.example.com:55443"
    assert parsed["restapi"]["enabled"] is True
    assert (
        parsed["notamDb"]["connectionUrl"]
        == "jdbc:h2:./Notams;mode=MySQL;AUTO_SERVER=TRUE"
    )


def test_load_config_reads_legacy_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "fnsClient.conf"
    config_path.write_text(
        "\n".join(
            [
                'jms.providerUrl="tcps://broker.example.com:55443"',
                'jms.destination="QUEUE"',
                'fil.sftp.host="sftp.example.com"',
                'fil.sftp.username="swim"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.jms.provider_url == "tcps://broker.example.com:55443"
    assert config.jms.destination == "QUEUE"
    assert config.fil.host == "sftp.example.com"

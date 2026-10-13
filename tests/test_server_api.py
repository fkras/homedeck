"""Tests for the HTTP API the Home Assistant add-on talks to.

The client is created without entering its context, so the lifespan - which
would start deck.py - never runs.
"""

import os
import sys

import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import server  # noqa: E402

client = TestClient(server.app)


class TestBaseConfiguration:
    def test_serves_the_base_file_verbatim(self):
        response = client.get(f'/v{server.API_VERSION}/base_configuration')

        assert response.status_code == 200
        with open(os.path.join('src', 'homedeck', 'yaml', 'configuration.base.yml'), encoding='utf-8') as fp:
            assert response.json()['data']['content'] == fp.read()

    def test_content_has_the_presets_the_editor_previews_with(self):
        content = client.get(f'/v{server.API_VERSION}/base_configuration').json()['data']['content']
        base = yaml.safe_load(content)

        assert '$default' in base['presets']
        assert set(base['system_buttons']) == {'$page.back', '$page.previous', '$page.next'}


class TestStatus:
    def test_head_is_routed_with_the_api_version(self):
        # Registered as a plain string once, it 404'd and broke discovery
        assert client.head(f'/v{server.API_VERSION}/status').status_code == 200

"""Scheduling changes retain the released tick's durable decisions."""

import hashlib
import json
from pathlib import Path

from . import scenario


def test_tick_decisions_match_the_released_reference(tmp_path):
    reference = json.loads(Path(__file__).with_name('baseline_decisions.json').read_text())
    assert reference['baselineHead'] == '90d5c535b719567661ab74125ff7648fba1d0440'
    assert hashlib.sha256(Path(scenario.__file__).read_bytes()).hexdigest() == reference['scenarioSha256']
    assert scenario.transcript(tmp_path / 'candidate') == reference['decisions']

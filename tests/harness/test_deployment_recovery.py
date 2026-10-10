"""Deployment recovery must poll known IDs and refuse ambiguous resubmission."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def deployment():
    path = Path(__file__).resolve().parents[2] / "scripts/deploy_studionet.py"
    spec = importlib.util.spec_from_file_location("deployment_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_saved_id_is_polled_without_resending(deployment, monkeypatch, tmp_path):
    state = {"deployment": {"transaction_id": "existing-tx"}}
    send = Mock()
    receipt = {"consensus_data": {"votes": {"v": "agree"}}}
    confirm = Mock(return_value=receipt)
    monkeypatch.setattr(deployment, "confirmed", confirm)
    deployment.send_once(Mock(), tmp_path / "recovery.json", state, "deployment", send)
    send.assert_not_called()
    assert confirm.call_args.args[1] == "existing-tx"


def test_lost_submission_response_is_durable_and_cannot_resubmit(deployment, tmp_path):
    state = {}
    send = Mock(side_effect=ConnectionError("response lost"))
    record = tmp_path / "recovery.json"
    with pytest.raises(ConnectionError):
        deployment.send_once(Mock(), record, state, "deployment", send)
    recovered = json.loads(record.read_text())
    assert "submission_started_at" in recovered["deployment"]
    with pytest.raises(RuntimeError, match="ambiguous"):
        deployment.send_once(Mock(), record, recovered, "deployment", send)
    assert send.call_count == 1


def test_polling_failure_keeps_id_for_resume(deployment, monkeypatch, tmp_path):
    state = {}
    record = tmp_path / "recovery.json"
    monkeypatch.setattr(deployment, "confirmed", Mock(side_effect=TimeoutError("pending")))
    with pytest.raises(TimeoutError):
        deployment.send_once(Mock(), record, state, "deployment", lambda: "known-tx")
    assert json.loads(record.read_text())["deployment"]["transaction_id"] == "known-tx"

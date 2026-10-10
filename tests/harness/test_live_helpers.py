"""Offline checks for ambiguous submissions and consensus evidence handling."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def live_helpers(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"
    spec = importlib.util.spec_from_file_location("live_helpers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    return module


def receipt(*, votes=None, succeeded=True, result=None):
    return {
        "consensus_data": {
            "votes": votes if votes is not None else {"validator": "agree"},
            "leader_receipt": [{
                "execution_result": "SUCCESS" if succeeded else "ERROR",
                "result": result,
            }],
        },
    }


@pytest.mark.parametrize("votes", [{}, {"v": "disagree"}, {"v": "timeout"}])
def test_absent_or_unfavorable_validator_evidence_fails(live_helpers, votes):
    with pytest.raises(AssertionError):
        live_helpers.assert_consensus(receipt(votes=votes))


def test_a_rejected_execution_can_still_have_consensus(live_helpers):
    assert live_helpers.assert_consensus(receipt(succeeded=False)) == {"validator": "agree"}


def test_an_ambiguous_submission_is_never_sent_again(live_helpers, monkeypatch):
    client = Mock()
    client.write_contract.side_effect = ConnectionError("submission response lost")
    monkeypatch.setattr(live_helpers, "get_gl_client", lambda: client)
    oracle = SimpleNamespace(address="contract", account="account")
    with pytest.raises(ConnectionError):
        live_helpers.transact(oracle, "submit_claim", ["m1"])
    assert client.write_contract.call_count == 1


def test_receipt_retry_preserves_the_submitted_id(live_helpers, monkeypatch):
    client = Mock()
    client.write_contract.return_value = "tx-id"
    client.wait_for_transaction_receipt.side_effect = [ConnectionError("connection reset"), receipt()]
    monkeypatch.setattr(live_helpers, "get_gl_client", lambda: client)
    monkeypatch.setattr(live_helpers.time, "sleep", lambda _: None)
    oracle = SimpleNamespace(address="contract", account="account")
    live_helpers.transact(oracle, "submit_claim", ["m1"])
    assert client.write_contract.call_count == 1
    assert client.wait_for_transaction_receipt.call_count == 2
    assert all(call.kwargs["transaction_hash"] == "tx-id" for call in client.wait_for_transaction_receipt.call_args_list)
    recovery = Path("artifacts/live-run.json").read_text()
    assert '"transaction_id": "tx-id"' in recovery


@pytest.mark.parametrize(
    "result,expected", [
        ({"status": "return", "payload": {"readable": '"m3"'}}, '"m3"'),
        ({"status": "rollback", "payload": "[EXPECTED] creator only"}, "[EXPECTED] creator only"),
    ],
)
def test_leader_results_are_unwrapped_using_the_installed_sdk_format(live_helpers, result, expected):
    assert live_helpers.leader_return(receipt(result=result)) == expected


def test_failed_triggered_transfer_fails_settlement_verification(live_helpers, monkeypatch):
    client = Mock()
    parent = receipt()
    parent["triggered_transactions"] = ["child-id"]
    client.wait_for_transaction_receipt.side_effect = [parent, receipt(succeeded=False)]
    monkeypatch.setattr(live_helpers, "get_gl_client", lambda: client)
    with pytest.raises(AssertionError):
        live_helpers.wait_receipt("parent-id", settle=True)


@pytest.mark.parametrize("credited", [True, False])
def test_studio_native_transfer_uses_value_credit_instead_of_consensus(live_helpers, monkeypatch, credited):
    client = Mock()
    parent = receipt()
    parent["triggered_transactions"] = ["child-id"]
    child = {"type": 0, "consensus_data": None, "value_credited": credited, "result_name": "NO_MAJORITY"}
    client.wait_for_transaction_receipt.side_effect = [parent, child]
    monkeypatch.setattr(live_helpers, "get_gl_client", lambda: client)
    if credited:
        assert live_helpers.wait_receipt("parent-id", settle=True) == parent
    else:
        with pytest.raises(AssertionError):
            live_helpers.wait_receipt("parent-id", settle=True)

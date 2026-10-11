"""Poll real driver paths with deterministic receipts; no network writes."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("milestone_client", Path("scripts/milestone_client.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Driver = module.MilestoneDriver


def receipt(result, status=7, success=True):
    return {"status": status, "consensus_data": {"leader_receipt": [{
        "execution_result": "SUCCESS" if success else "ERROR", "result": {
            "status": "return" if success else "rollback", "payload": {
                "readable": json.dumps(result)}}}]}, "triggered_transactions": []}


class Client:
    local_account = SimpleNamespace(address="0xAlice")
    def __init__(self):
        self.transactions = {}
        self.calls = []
        self.writes = []
        self.milestone = {"status": "deferred", "attempts": 1}
        self.refundable = False
        self.resolvable = False
    def write_contract(self, **kwargs):
        self.writes.append(kwargs)
        return "tx" + str(len(self.writes))
    def get_transaction(self, tx):
        self.calls.append(tx)
        return self.transactions.get(tx)
    def read_contract(self, **kwargs):
        method = kwargs["function_name"]
        if method == "get_milestone": return dict(self.milestone)
        if method == "is_refundable": return {"refundable": self.refundable}
        if method == "is_resolvable": return {"resolvable": self.resolvable, "reason": "contract is paused"}
        raise AssertionError(method)


def test_stored_appeal_is_polled_and_final_ruling_refreshes_out_of_appeal(tmp_path):
    client = Client()
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    driver.submit("resolve", "resolve_deferred", ["m1"], milestone_id="m1")
    driver.attach_appeal("resolve", "appeal1")
    client.transactions.update(tx1=receipt({}, status=9), appeal1=receipt({}, status=9))
    assert driver.poll("resolve")["stage"] == "appeal_pending"
    assert client.calls == ["appeal1", "tx1"]
    restarted = Driver(client, "0xOracle", path)
    client.transactions.update(tx1=receipt({"status": "achieved"}), appeal1=receipt({}))
    client.milestone = {"status": "achieved", "attempts": 2}
    result = restarted.poll("resolve")
    assert result["stage"] == "confirmed"
    assert result["milestone"]["status"] == "achieved"
    assert len(client.writes) == 1
    assert restarted.state["operations"]["resolve"]["appeal_transaction_id"] == "appeal1"


def test_lost_submission_response_persists_ambiguity_and_never_retries(tmp_path):
    client = Client()
    def lost_response(**kwargs):
        client.writes.append(kwargs)
        raise ConnectionError("response lost")
    client.write_contract = lost_response
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    with pytest.raises(ConnectionError): driver.submit("create", "create_milestone", [])
    restarted = Driver(client, "0xOracle", path)
    with pytest.raises(RuntimeError, match="ambiguous"):
        restarted.submit("create", "create_milestone", [])
    assert len(client.writes) == 1


@pytest.mark.parametrize("state,stage", [("open", "awaiting_claim"), ("achieved", "terminal"),
    ("cancelled", "terminal"), ("refunded", "terminal"), ("deferred", "waiting"), ("contested", "waiting")])
def test_polling_handles_all_states_without_missing_timestamp_keys(tmp_path, state, stage):
    client = Client()
    client.milestone["status"] = state
    result = Driver(client, "0xOracle", tmp_path / "journal.json").drive("m1")
    assert result["stage"] == stage
    assert not client.writes


def test_existing_transaction_is_resumed_and_changed_intent_is_refused(tmp_path):
    client = Client()
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    driver.submit("create", "create_milestone", [1])
    restarted = Driver(client, "0xOracle", path)
    assert restarted.submit("create", "create_milestone", [1]) == "tx1"
    with pytest.raises(ValueError, match="different arguments"):
        restarted.submit("create", "create_milestone", [2])
    assert len(client.writes) == 1


def test_failed_finalized_execution_is_not_confirmed(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    client.transactions["tx1"] = receipt({}, success=False)
    assert driver.poll("claim")["stage"] == "failed"


def test_resolvable_case_polls_saved_transaction_then_refreshes_chain_state(tmp_path):
    client = Client()
    client.resolvable = True
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    assert driver.drive("m1")["stage"] == "pending"
    assert client.writes[0]["function_name"] == "resolve_deferred"
    client.transactions["tx1"] = receipt({"status": "contested"})
    client.milestone["status"] = "contested"
    client.milestone["attempts"] = 2
    assert driver.poll("m1.resolve.2")["milestone"]["status"] == "contested"


def test_expired_claim_submits_permissionless_refund_and_refreshes(tmp_path):
    client = Client()
    client.refundable = True
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    assert driver.drive("m1")["stage"] == "pending"
    client.transactions["tx1"] = receipt({"status": "refunded"})
    client.milestone["status"] = "refunded"
    assert driver.poll("m1.refund")["milestone"]["status"] == "refunded"


def test_missing_stored_appeal_receipt_keeps_case_in_appeal(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    driver.attach_appeal("claim", "appeal1")
    client.transactions["tx1"] = receipt({"status": "achieved"})
    assert driver.poll("claim")["stage"] == "appeal_pending"
    assert client.calls == ["appeal1", "tx1"]


def test_failed_finalized_appeal_refreshes_ruling_out_of_appealed_state(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    driver.attach_appeal("claim", "appeal1")
    client.transactions.update(tx1=receipt({}, success=False), appeal1=receipt({}, success=False))
    client.milestone = {"status": "open", "attempts": 0}
    result = driver.poll("claim")
    assert result["stage"] == "failed"
    assert result["milestone"]["status"] == "open"
    assert result["appeal_transaction_id"] == "appeal1"


def test_cancelled_appeal_resumes_original_final_ruling_after_restart(tmp_path):
    client = Client()
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    driver.attach_appeal("claim", "appeal1")
    client.transactions.update(tx1=receipt({}, status=9), appeal1=receipt({}, status=9))
    assert driver.poll("claim")["stage"] == "appeal_pending"
    client.transactions.update(tx1=receipt({"status": "achieved"}),
                               appeal1=receipt({}, status=module.CANCELED))
    client.milestone = {"status": "achieved", "attempts": 1}
    restarted = Driver(client, "0xOracle", path)
    # Both receipts are terminal, so wait must not request impossible finalization
    # of the cancelled appeal. Client deliberately has no network wait method.
    result = restarted.wait("claim")
    assert result["stage"] == "confirmed"
    assert result["appeal_stage"] == "cancelled"
    assert result["milestone"]["status"] == "achieved"
    assert len(client.writes) == 1


def test_restart_drive_polls_existing_pending_recheck_without_another_write(tmp_path):
    client = Client()
    client.resolvable = True
    path = tmp_path / "journal.json"
    Driver(client, "0xOracle", path).drive("m1")
    restarted = Driver(client, "0xOracle", path)
    assert restarted.drive("m1")["stage"] == "pending"
    assert len(client.writes) == 1


class PaymentClient(Client):
    def __init__(self):
        super().__init__()
        self.payment = {"payment_id": "p1", "status": "queued", "amount_atto": 100,
                        "to": self.local_account.address, "recipient_kind": "eoa"}
        self.fail_child_once = True
    def write_contract(self, **kwargs):
        tx = super().write_contract(**kwargs)
        if kwargs["function_name"] == "withdraw_credit":
            self.transactions[tx] = receipt(dict(self.payment))
            self.transactions[tx]["triggered_transactions"] = ["native-child"]
        else:
            assert kwargs["args"] == ["p1", "tx1"]
            self.payment.update(status="delivered", value_credited=True, application_status="not_applicable")
            self.transactions[tx] = receipt(dict(self.payment))
        return tx
    def wait_for_transaction_receipt(self, tx, **kwargs):
        if tx == "native-child" and self.fail_child_once:
            self.fail_child_once = False
            raise TimeoutError("child polling interrupted")
        return self.transactions.get(tx)
    def read_contract(self, **kwargs):
        if kwargs["function_name"] == "get_payment": return dict(self.payment)
        return super().read_contract(**kwargs)


def test_withdrawal_restart_polls_saved_child_then_confirms_without_resending(tmp_path):
    client = PaymentClient()
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    with pytest.raises(TimeoutError):
        driver.withdraw("payment", client.local_account.address, 100)
    journal = json.loads(path.read_text())
    assert journal["operations"]["payment"]["transaction_id"] == "tx1"
    assert journal["operations"]["payment"]["child_transaction_ids"] == ["native-child"]
    assert journal["operations"]["payment"]["payment_id"] == "p1"
    restarted = Driver(client, "0xOracle", path)
    assert restarted.withdraw("payment", client.local_account.address, 100)["status"] == "delivered"
    assert [w["function_name"] for w in client.writes] == ["withdraw_credit", "confirm_payment"]
    assert restarted.withdraw("payment", client.local_account.address, 100)["status"] == "delivered"
    assert len(client.writes) == 2


def test_external_confirmation_is_read_without_duplicate_confirmation_write(tmp_path):
    client = PaymentClient()
    client.fail_child_once = False
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("payment", "withdraw_credit", [client.local_account.address, 100, "eoa"])
    client.payment.update(status="delivered", value_credited=True)
    assert driver.withdraw("payment", client.local_account.address, 100)["status"] == "delivered"
    assert len(client.writes) == 1


def test_cancelled_network_transaction_leaves_pending_state_without_resending(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    client.transactions["tx1"] = receipt({}, status=8, success=False)
    result = driver.poll("claim")
    assert result["stage"] == "cancelled"
    assert result["execution_succeeded"] is False
    assert len(client.writes) == 1


def test_same_protocol_transaction_id_appeal_is_polled_until_final_ruling(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    driver.attach_appeal("claim", "tx1")
    client.transactions["tx1"] = receipt({}, status=10)
    assert driver.poll("claim")["stage"] == "appeal_pending"
    client.transactions["tx1"] = receipt({"status": "achieved"})
    client.milestone["status"] = "achieved"
    result = driver.poll("claim")
    assert result["stage"] == "confirmed"
    assert result["milestone"]["status"] == "achieved"
    assert len(client.writes) == 1


def test_parent_remains_appealed_when_separate_appeal_submission_has_finalized(tmp_path):
    client = Client()
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.submit("claim", "submit_claim", ["m1"], milestone_id="m1")
    driver.attach_appeal("claim", "appeal1")
    client.transactions.update(tx1=receipt({}, status=9), appeal1=receipt({}))
    assert driver.poll("claim")["stage"] == "appeal_pending"


class FailedConfirmationClient(PaymentClient):
    def __init__(self):
        super().__init__()
        self.fail_child_once = False
        self.fail_confirmation_once = True
    def write_contract(self, **kwargs):
        if kwargs["function_name"] == "confirm_payment" and self.fail_confirmation_once:
            self.fail_confirmation_once = False
            self.writes.append(kwargs)
            tx = "tx" + str(len(self.writes))
            self.transactions[tx] = receipt("[TRANSIENT] receipt RPC unavailable", success=False)
            return tx
        return super().write_contract(**kwargs)


def test_finalized_failed_confirmation_can_resume_without_repeating_withdrawal(tmp_path):
    client = FailedConfirmationClient()
    path = tmp_path / "journal.json"
    driver = Driver(client, "0xOracle", path)
    with pytest.raises(RuntimeError, match="confirmation failed"):
        driver.withdraw("payment", client.local_account.address, 100)
    assert driver.state["operations"]["payment.confirm"]["stage"] == "failed"
    restarted = Driver(client, "0xOracle", path)
    payment = restarted.withdraw("payment", client.local_account.address, 100)
    assert payment["status"] == "delivered"
    assert [w["function_name"] for w in client.writes] == ["withdraw_credit", "confirm_payment", "confirm_payment"]
    assert restarted.state["operations"]["payment.confirm"]["transaction_id"] == "tx2"
    assert restarted.state["operations"]["payment.confirm.retry.1"]["transaction_id"] == "tx3"


def test_known_failed_recheck_can_retry_after_authoritative_eligibility_refresh(tmp_path):
    client = Client()
    client.resolvable = True
    driver = Driver(client, "0xOracle", tmp_path / "journal.json")
    driver.drive("m1")
    client.transactions["tx1"] = receipt("[TRANSIENT] source unavailable", success=False)
    assert driver.drive("m1")["stage"] == "failed"
    assert driver.drive("m1")["stage"] == "pending"
    assert len(client.writes) == 2
    assert driver.state["operations"]["m1.resolve.2.retry.1"]["transaction_id"] == "tx2"

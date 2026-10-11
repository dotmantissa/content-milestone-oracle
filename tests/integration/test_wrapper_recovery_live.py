"""An IC depositor can recover a true failed deposit using its own sender identity."""
from pathlib import Path

from gltest.assertions import tx_execution_succeeded
from gltest.utils import extract_contract_address
from genlayer_py.types import TransactionStatus

from conftest import CTOR, REWARD, checkpoint, leader_return, retry_rpc, wait_receipt
import json


def test_wrapper_recovers_finalized_vm_fault_deposit_and_redeems(gl_client):
    from conftest import deployer_account
    account = deployer_account() or gl_client.local_account
    source = Path("contracts/content_milestone_oracle.py").read_text()
    original = '''    def deposit(self) -> int:
        """Unconditional deposit to the immediate sender's recoverable credit."""
        self._credit(gl.message.sender_address, int(gl.message.value))
        return int(self.credits.get(gl.message.sender_address, u256(0)))'''
    faulty = '''    def deposit(self) -> int:
        raise gl.vm.UserError("[EXPECTED] deliberate deposit fault fixture")'''
    assert original in source
    fault_source = source.replace(original, faulty, 1).encode()
    # Isolated test fixture, never the production deployment or advertised source.
    tx = gl_client.deploy_contract(code=fault_source, args=CTOR, account=account)
    checkpoint("fault_fixture_deploy", tx)
    receipt = wait_receipt(tx, settle=True)
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    oracle = extract_contract_address(receipt)
    tx = gl_client.deploy_contract(code=Path("contracts/grant_programme.py").read_bytes(), args=[oracle], account=account)
    checkpoint("recovery_wrapper_deploy", tx)
    receipt = wait_receipt(tx, settle=True)
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    wrapper = extract_contract_address(receipt)

    def call(method, args, value=0):
        txid = gl_client.write_contract(address=wrapper, function_name=method, args=args, value=value, account=account)
        checkpoint("recovery_wrapper_" + method, txid, address=wrapper)
        parent = retry_rpc(lambda: gl_client.wait_for_transaction_receipt(txid,
            status=TransactionStatus.FINALIZED, interval=6000, retries=100))
        assert tx_execution_succeeded(parent), leader_return(parent)
        child_id = parent["triggered_transactions"][0]
        child = retry_rpc(lambda: gl_client.wait_for_transaction_receipt(child_id,
            status=TransactionStatus.FINALIZED, interval=6000, retries=100))
        return child_id, child

    failed_id, failed = call("fund_oracle", [], REWARD)
    assert not tx_execution_succeeded(failed) and failed["value_credited"] is True
    def budget():
        return gl_client.read_contract(address=wrapper, function_name="budget", args=[])
    assert budget()["oracle_credit_atto"] == 0
    _, recovered = call("recover_oracle_deposit", [failed_id])
    assert tx_execution_succeeded(recovered), leader_return(recovered)
    assert budget()["oracle_credit_atto"] == REWARD
    before = gl_client.get_balance(account.address)
    withdrawal_id, withdrawal = call("withdraw_budget", [account.address, REWARD, "eoa"])
    assert tx_execution_succeeded(withdrawal), leader_return(withdrawal)
    for native_child in withdrawal["triggered_transactions"]:
        credited = wait_receipt(native_child, settle=True)
        assert credited["value_credited"] is True
    ticket = json.loads(leader_return(withdrawal))
    tx = gl_client.write_contract(address=oracle, function_name="confirm_payment",
        args=[ticket["payment_id"], withdrawal_id], account=account)
    checkpoint("wrapper_fault_payment_confirmation", tx, address=oracle)
    assert tx_execution_succeeded(wait_receipt(tx, settle=True))
    assert budget()["oracle_credit_atto"] == 0
    assert gl_client.get_balance(account.address) == before + REWARD

"""Live regression evidence for recovered funds, finalized payments and IC composition."""
import json
from pathlib import Path
import time

import pytest
from gltest.assertions import tx_execution_succeeded
from gltest.utils import extract_contract_address

from conftest import (DAY, GITHUB_URL, REWARD, assert_consensus, checkpoint,
                      create_milestone, leader_return, now_ts, pace, read, transact, wait_receipt)


def result(receipt):
    return json.loads(leader_return(receipt))


def withdraw(oracle, beneficiary, gl_client, *, recipient=None, kind="eoa", label="withdraw"):
    from scripts.milestone_client import MilestoneDriver
    address = beneficiary.account.address
    amount = read(oracle.credit_of(args=[address]))
    driver = MilestoneDriver(gl_client, oracle.address,
        Path("artifacts/remediation") / ("payments-" + oracle.address + "-" + address + ".json"))
    ticket = driver.withdraw(label, recipient or address, amount, recipient_kind=kind, account=beneficiary.account)
    assert ticket["status"] == "delivered", ticket
    return ticket


def test_rejected_funding_is_sender_credit_and_redeems(oracle, gl_client, accounts):
    before = gl_client.get_balance(oracle.account.address)
    receipt = transact(oracle, "create_milestone", [accounts[1].address,
        "https://www.youtube.com/watch?v=x", "github", "stars", 100,
        now_ts(oracle) + DAY, "Rejected funding recovery", 0, 0, 0], value=REWARD)
    assert tx_execution_succeeded(receipt)
    rejection = result(receipt)
    assert rejection["status"] == "rejected", rejection
    assert read(oracle.credit_of(args=[oracle.account.address])) == REWARD
    assert read(oracle.milestone_count(args=[])) == 0
    assert read(oracle.get_treasury(args=[]))["owner_withdrawable_atto"] == 0
    payment = withdraw(oracle, oracle, gl_client, label="rejected_funding")
    assert payment["amount_atto"] == REWARD
    assert read(oracle.credit_of(args=[oracle.account.address])) == 0
    assert gl_client.get_balance(oracle.account.address) == before


def test_deployed_comparator_rejects_both_boundary_orders(oracle, accounts):
    mid = create_milestone(oracle, accounts[1].address, threshold=10_000)
    for a, b in [(10100, 10099), (10099, 10100), (9899, 9900), (9900, 9899)]:
        comparison = read(oracle.preview_agreement(args=[mid, a, b]))
        assert comparison["agree"] is False, comparison
    cancelled = transact(oracle, "cancel_milestone", [mid], settle=True)
    assert tx_execution_succeeded(cancelled)


def test_actual_vm_fault_deposit_recovers_once_from_final_receipt(oracle, gl_client):
    # Unknown method causes a real runner rollback before any credit ledger entry.
    tx = gl_client.write_contract(address=oracle.address, function_name="missing_payable_method",
                                  args=[], value=REWARD, account=oracle.account)
    checkpoint("vm_fault", tx, address=oracle.address)
    failed = wait_receipt(tx, settle=True)
    assert not tx_execution_succeeded(failed)
    assert failed["value_credited"] is True, failed
    recovered = transact(oracle, "recover_failed_deposit", [str(tx)])
    assert tx_execution_succeeded(recovered), leader_return(recovered)
    assert result(recovered)["amount_atto"] == REWARD
    replay = transact(oracle, "recover_failed_deposit", [str(tx)])
    assert not tx_execution_succeeded(replay)
    payment = withdraw(oracle, oracle, gl_client, label="vm_fault_recovery")
    assert payment["amount_atto"] == REWARD


def test_recovery_donation_pool_is_separate_from_failed_depositor_funds(oracle, gl_client):
    donation = transact(oracle, "fund_recovery", [], value=REWARD)
    assert tx_execution_succeeded(donation)
    assert read(oracle.get_treasury(args=[]))["recovery_funding_atto"] == REWARD
    tx = gl_client.write_contract(address=oracle.address, function_name="missing_payable_method",
                                  args=[], value=REWARD, account=oracle.account)
    checkpoint("isolated_pool_vm_fault", tx, address=oracle.address)
    failed = wait_receipt(tx, settle=True)
    assert not tx_execution_succeeded(failed) and failed["value_credited"] is True
    recovered = transact(oracle, "recover_failed_deposit", [str(tx)])
    assert tx_execution_succeeded(recovered)
    assert read(oracle.credit_of(args=[oracle.account.address])) == REWARD
    withdraw(oracle, oracle, gl_client, label="isolated_pool_deposit_recovery")
    treasury = read(oracle.get_treasury(args=[]))
    assert treasury["balance_atto"] == treasury["recovery_funding_atto"] == REWARD
    assert treasury["credits_atto"] == treasury["reserved_atto"] == 0
    assert treasury["solvent"] is True


@pytest.mark.parametrize("metric,threshold", [("forks", 1), ("watchers", 1), ("issues", 1), ("contributors", 1)])
def test_github_additional_metrics_have_live_independent_readings(oracle, accounts, gl_client, metric, threshold):
    creator = oracle.connect(accounts[1])
    mid = create_milestone(oracle, accounts[1].address, threshold=threshold, metric=metric)
    receipt = transact(creator, "submit_claim", [mid])
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    reading = read(oracle.get_milestone(args=[mid]))
    assert reading["status"] == "achieved", reading
    assert reading["observed_value"] >= threshold
    withdraw(oracle, creator, gl_client, label="github_" + metric)


def test_npm_official_downloads_api_is_read_live(oracle, accounts, gl_client):
    creator = oracle.connect(accounts[1])
    mid = create_milestone(oracle, accounts[1].address, threshold=1000,
        url="https://api.npmjs.org/downloads/point/last-week/react", platform="npm", metric="downloads")
    receipt = transact(creator, "submit_claim", [mid])
    assert tx_execution_succeeded(receipt), leader_return(receipt)
    assert read(oracle.get_milestone(args=[mid]))["status"] == "achieved"
    withdraw(oracle, creator, gl_client, label="npm_downloads")


def test_wrapper_funding_creation_cancellation_and_internal_refund_lifecycle(oracle, gl_client):
    # Finalized parent/child message hops can exceed the general 60s fixture
    # cancellation window. Set the terms before funding this wrapper's grant.
    configured = transact(oracle, "set_economics", [0, 365 * DAY, 3600])
    assert tx_execution_succeeded(configured), leader_return(configured)
    code = Path("contracts/grant_programme.py").read_bytes()
    tx = gl_client.deploy_contract(code=code, args=[oracle.address], account=oracle.account)
    checkpoint("wrapper_deploy", tx)
    deployed = wait_receipt(tx, settle=True)
    assert tx_execution_succeeded(deployed), leader_return(deployed)
    wrapper = extract_contract_address(deployed)
    def view(method, args=[]):
        return gl_client.read_contract(address=wrapper, function_name=method, args=args)
    def call(method, args, *, value=0):
        txid = gl_client.write_contract(address=wrapper, function_name=method, args=args,
                                       value=value, account=oracle.account)
        checkpoint("wrapper_" + method, txid, address=wrapper)
        receipt = wait_receipt(txid, settle=True)
        assert tx_execution_succeeded(receipt), leader_return(receipt)
        for child in receipt.get("triggered_transactions", []):
            child_receipt = wait_receipt(child, settle=True)
            assert tx_execution_succeeded(child_receipt), leader_return(child_receipt)
            for grandchild in child_receipt.get("triggered_transactions", []):
                final_child = wait_receipt(grandchild, settle=True)
                if final_child.get("consensus_data") is None:
                    assert final_child["value_credited"] is True, final_child
                else:
                    assert tx_execution_succeeded(final_child), leader_return(final_child)
        return receipt
    call("fund_oracle", [], value=REWARD)
    assert view("budget")["oracle_credit_atto"] == REWARD
    created = call("create_grant", [oracle.account.address, GITHUB_URL, 5000,
                                    now_ts(oracle) + DAY, REWARD])
    child = wait_receipt(created["triggered_transactions"][0], settle=True)
    mid = result(child)
    assert read(oracle.get_milestone(args=[mid]))["sponsor"].lower() == wrapper.lower()
    call("cancel_grant", [mid])
    assert view("budget")["oracle_credit_atto"] == REWARD
    withdrawn = call("withdraw_budget", [wrapper, REWARD, "ic"])
    withdrawal_tx = withdrawn["triggered_transactions"][0]
    payment = result(wait_receipt(withdrawal_tx, settle=True))
    confirmation = transact(oracle, "confirm_payment", [payment["payment_id"], withdrawal_tx])
    assert tx_execution_succeeded(confirmation), leader_return(confirmation)
    assert result(confirmation)["recipient_execution"] == "SUCCESS"
    budget = view("budget")
    assert budget["oracle_credit_atto"] == 0
    assert budget["received_atto"] == REWARD and budget["native_balance_atto"] == REWARD
    # Callback funds must remain usable: recycle them to oracle credit, then
    # redeem to an EOA with finalized evidence and an actual balance increase.
    call("redeposit_budget", [REWARD])
    assert view("budget")["oracle_credit_atto"] == REWARD
    assert view("budget")["native_balance_atto"] == 0
    before = gl_client.get_balance(oracle.account.address)
    redeemed = call("withdraw_budget", [oracle.account.address, REWARD, "eoa"])
    withdrawal_tx = redeemed["triggered_transactions"][0]
    payment = result(wait_receipt(withdrawal_tx, settle=True))
    confirmation = transact(oracle, "confirm_payment", [payment["payment_id"], withdrawal_tx])
    assert tx_execution_succeeded(confirmation), leader_return(confirmation)
    assert result(confirmation)["status"] == "delivered"
    assert view("budget")["oracle_credit_atto"] == 0
    assert gl_client.get_balance(oracle.account.address) == before + REWARD


def test_failed_ic_application_is_reported_without_replaying_native_credit(oracle, gl_client):
    code = Path("tests/fixtures/rejecting_recipient.py").read_bytes()
    tx = gl_client.deploy_contract(code=code, args=[], account=oracle.account)
    checkpoint("rejecting_recipient_deploy", tx)
    deployed = wait_receipt(tx, settle=True)
    assert tx_execution_succeeded(deployed), leader_return(deployed)
    recipient = extract_contract_address(deployed)
    funded = transact(oracle, "deposit", [], value=REWARD)
    assert tx_execution_succeeded(funded)
    ticket = withdraw(oracle, oracle, gl_client, recipient=recipient, kind="ic", label="failed_ic_application")
    assert ticket["value_credited"] is True
    assert ticket["recipient_execution"] == "ERROR"
    assert ticket["application_status"] == "failed"
    receiver_balance = gl_client.read_contract(address=recipient, function_name="native_balance", args=[])
    assert receiver_balance == REWARD
    denied = transact(oracle, "restore_failed_payment", [ticket["payment_id"]])
    assert not tx_execution_succeeded(denied)

"""Durable transaction and milestone polling, including appeals and child transfers.

Run ``python3 scripts/milestone_client.py --help``. Journals contain public
transaction metadata only. An ambiguous submission is never automatically sent
again. A pending or appealed operation is always resumed using its stored IDs.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from genlayer_py import create_account, create_client
from genlayer_py.chains import localnet
from genlayer_py.types import TransactionStatus, TransactionHashVariant, TRANSACTION_STATUS_NAME_TO_NUMBER
from gltest.assertions import tx_execution_succeeded

RPC = "https://studio.genlayer.com/api"
FINALIZED = int(TRANSACTION_STATUS_NAME_TO_NUMBER[TransactionStatus.FINALIZED])
CANCELED = int(TRANSACTION_STATUS_NAME_TO_NUMBER[TransactionStatus.CANCELED])
TERMINAL_TRANSACTIONS = {FINALIZED, CANCELED}
APPEAL_STATUSES = {int(TRANSACTION_STATUS_NAME_TO_NUMBER[TransactionStatus.APPEAL_COMMITTING]),
                  int(TRANSACTION_STATUS_NAME_TO_NUMBER[TransactionStatus.APPEAL_REVEALING])}


def leader_value(receipt):
    result = receipt["consensus_data"]["leader_receipt"][0]["result"]
    if result.get("status") != "return":
        raise RuntimeError("Contract execution did not return successfully")
    payload = result["payload"]
    readable = payload.get("readable") if isinstance(payload, dict) else payload
    return json.loads(readable) if isinstance(readable, str) else readable


class MilestoneDriver:
    def __init__(self, client, address, journal):
        self.client = client
        self.address = address
        self.path = Path(journal)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "contract": address, "operations": {}}
        if self.state["contract"].lower() != address.lower():
            raise ValueError("Journal belongs to another contract")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.state, indent=2, default=str) + "\n")
        temporary.replace(self.path)

    def read(self, method, args):
        return self.client.read_contract(address=self.address, function_name=method, args=args,
            transaction_hash_variant=TransactionHashVariant.LATEST_FINAL)

    def submit(self, operation, method, args, *, value=0, account=None, milestone_id=None):
        account = account or self.client.local_account
        intent = dict(method=method, args=args, value=value, sender=account.address)
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True, default=str).encode()).hexdigest()
        existing = self.state["operations"].get(operation)
        if existing is not None:
            if existing["intent_hash"] != fingerprint:
                raise ValueError("Saved operation has different arguments or sender")
            if "transaction_id" not in existing:
                raise RuntimeError("Submission is ambiguous; identify the original transaction before resuming")
            return existing["transaction_id"]
        saved = {**intent, "intent_hash": fingerprint, "stage": "submitting"}
        if milestone_id is not None:
            saved["milestone_id"] = milestone_id
        self.state["operations"][operation] = saved
        self.save()  # Record intent before a response can be lost.
        tx_id = str(self.client.write_contract(address=self.address, function_name=method,
            args=args, value=value, account=account))
        saved.update(transaction_id=tx_id, stage="pending")
        self.save()
        return tx_id

    def attach_appeal(self, operation, transaction_id):
        self.state["operations"][operation]["appeal_transaction_id"] = transaction_id
        self.save()

    def poll(self, operation):
        saved = self.state["operations"][operation]
        if "transaction_id" not in saved:
            raise RuntimeError("Submission is ambiguous; no transaction ID is recorded")
        appeal = None
        if saved.get("appeal_transaction_id"):
            appeal = self.client.get_transaction(saved["appeal_transaction_id"])
        receipt = self.client.get_transaction(saved["transaction_id"])
        appeal_pending = bool(saved.get("appeal_transaction_id")) and (appeal is None or int(appeal["status"]) not in TERMINAL_TRANSACTIONS)
        if appeal is not None:
            saved["appeal_stage"] = "cancelled" if int(appeal["status"]) == CANCELED else "finalized" if int(appeal["status"]) == FINALIZED else "pending"
        if receipt is None or appeal_pending:
            saved["stage"] = "appeal_pending" if appeal_pending else "pending"
        elif int(receipt["status"]) == CANCELED:
            saved["stage"] = "cancelled"
            saved["execution_succeeded"] = False
        elif int(receipt["status"]) != FINALIZED:
            saved["stage"] = "appeal_pending" if int(receipt["status"]) in APPEAL_STATUSES else "pending"
        elif not tx_execution_succeeded(receipt):
            saved["stage"] = "failed"
            saved["execution_succeeded"] = False
            leaders = (receipt.get("consensus_data") or {}).get("leader_receipt", [])
            saved["execution_error"] = leaders[0].get("result") if leaders else "missing execution receipt"
        else:
            saved["stage"] = "confirmed"
            saved["execution_succeeded"] = True
            saved["result"] = leader_value(receipt)
            if saved["method"] in ("create_milestone", "create_from_credit"):
                if isinstance(saved["result"], str):
                    saved["milestone_id"] = saved["result"]
                else:
                    saved["stage"] = "rejected"
        if receipt is not None and int(receipt["status"]) in TERMINAL_TRANSACTIONS and saved.get("milestone_id"):
            # Failed appeals can also change the authoritative milestone state.
            saved["milestone"] = self.read("get_milestone", [saved["milestone_id"]])
        self.save()
        return dict(saved)

    def wait(self, operation):
        saved = self.state["operations"][operation]
        for tx_id in dict.fromkeys([saved["transaction_id"], saved.get("appeal_transaction_id")]):
            if not tx_id:
                continue
            current = self.client.get_transaction(tx_id)
            if current is None or int(current["status"]) not in TERMINAL_TRANSACTIONS:
                self.client.wait_for_transaction_receipt(tx_id,
                    status=TransactionStatus.FINALIZED, interval=6000, retries=100)
        return self.poll(operation)

    def drive(self, milestone_id):
        for name, operation in self.state["operations"].items():
            if operation.get("milestone_id") == milestone_id and operation["stage"] in ("submitting", "pending", "appeal_pending"):
                return self.poll(name)
        record = self.read("get_milestone", [milestone_id])
        if record["status"] in ("achieved", "cancelled", "refunded"):
            return {"stage": "terminal", "milestone": record}
        refundable = self.read("is_refundable", [milestone_id])
        if refundable["refundable"]:
            name = self.retry_name(milestone_id + ".refund")
            self.submit(name, "refund", [milestone_id], milestone_id=milestone_id)
            return self.poll(name)
        if record["status"] in ("deferred", "contested"):
            predicate = self.read("is_resolvable", [milestone_id])
            if predicate["resolvable"]:
                name = self.retry_name(milestone_id + ".resolve." + str(record["attempts"] + 1))
                self.submit(name, "resolve_deferred", [milestone_id], milestone_id=milestone_id)
                return self.poll(name)
            return {"stage": "waiting", "reason": predicate["reason"],
                    "recheck_after": predicate.get("recheck_after"), "milestone": record}
        return {"stage": "awaiting_claim", "milestone": record}

    def retry_name(self, base):
        """A new explicit invocation may retry a proven failed read/exit action."""
        name, attempt = base, 0
        while self.state["operations"].get(name, {}).get("stage") in ("failed", "cancelled"):
            attempt += 1
            name = base + ".retry." + str(attempt)
        return name  # Pending, successful or ambiguous operations keep their ID.

    def withdraw(self, operation, to, amount, *, recipient_kind="eoa", account=None):
        self.submit(operation, "withdraw_credit", [to, amount, recipient_kind], account=account)
        withdrawal = self.wait(operation)
        if withdrawal["stage"] != "confirmed":
            raise RuntimeError("Withdrawal execution failed")
        ticket = withdrawal["result"]
        saved = self.state["operations"][operation]
        saved["payment_id"] = ticket["payment_id"]
        current = self.read("get_payment", [ticket["payment_id"]])
        if current["status"] != "queued":
            saved["payment"] = current
            self.save()
            return current  # Another caller may already have confirmed it; never replay.
        parent = self.client.get_transaction(withdrawal["transaction_id"])
        if parent is None or len(parent.get("triggered_transactions", [])) != 1:
            raise RuntimeError("Withdrawal child receipt is unavailable; resume the stored operation")
        saved["child_transaction_ids"] = list(parent["triggered_transactions"])
        self.save()
        for child in saved["child_transaction_ids"]:
            self.client.wait_for_transaction_receipt(child, status=TransactionStatus.FINALIZED,
                interval=6000, retries=100)
        confirm_name = self.retry_name(operation + ".confirm")
        self.submit(confirm_name, "confirm_payment", [ticket["payment_id"], withdrawal["transaction_id"]], account=account)
        confirmation = self.wait(confirm_name)
        if confirmation["stage"] != "confirmed":
            current = self.read("get_payment", [ticket["payment_id"]])
            if current["status"] != "queued":
                saved["payment"] = current
                self.save()
                return current
            raise RuntimeError("Payment confirmation failed; retain the existing ticket and transaction IDs")
        payment = self.read("get_payment", [ticket["payment_id"]])
        saved["payment"] = payment
        self.save()
        return payment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("--journal", type=Path, default=Path("artifacts/client-journal.json"))
    parser.add_argument("--milestone", help="Drive a recheck/refund lifecycle once")
    parser.add_argument("--resume", help="Poll an existing operation; never resend")
    parser.add_argument("--appeal-id", help="Attach an already submitted appeal to --resume")
    parser.add_argument("--withdraw-to")
    parser.add_argument("--amount", type=int)
    parser.add_argument("--recipient-kind", choices=["eoa", "ic"], default="eoa")
    parser.add_argument("--operation", default="withdraw")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    client = create_client(chain=deepcopy(localnet), endpoint=RPC,
        account=create_account(account_private_key=os.environ["DEPLOYER_PRIVATE_KEY"]))
    driver = MilestoneDriver(client, args.address, args.journal)
    if args.resume:
        if args.appeal_id:
            driver.attach_appeal(args.resume, args.appeal_id)
        result = driver.poll(args.resume)
    elif args.withdraw_to:
        if args.amount is None:
            parser.error("--amount is required for withdrawal")
        result = driver.withdraw(args.operation, args.withdraw_to, args.amount, recipient_kind=args.recipient_kind)
    elif args.milestone:
        result = driver.drive(args.milestone)
    else:
        parser.error("choose --milestone, --resume or --withdraw-to")
    print(json.dumps(result, indent=2, default=str))
    if (result.get("stage") in ("failed", "cancelled", "rejected")
            or result.get("status") == "failed" or result.get("application_status") == "failed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

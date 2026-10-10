"""Deploy and verify the production defaults, with saved transaction recovery.

    python3 scripts/deploy_studionet.py --smoke-test
    python3 scripts/deploy_studionet.py --resume --smoke-test

Loads DEPLOYER_PRIVATE_KEY from the environment or ignored .env. Recovery
metadata contains only public addresses, IDs, source hashes and verification.
"""

import argparse
import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv
from genlayer_py import create_account, create_client
from genlayer_py.chains import localnet
from genlayer_py.types import TransactionStatus
from gltest.assertions import tx_execution_succeeded
from gltest.utils import extract_contract_address

ROOT = Path(__file__).resolve().parents[1]
STUDIO_RPC = "https://studio.genlayer.com/api"
SOURCE = ROOT / "contracts/content_milestone_oracle.py"


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(path)


def confirmed(client, tx_id, *, finalized=False):
    receipt = client.wait_for_transaction_receipt(
        tx_id, status=TransactionStatus.FINALIZED if finalized else TransactionStatus.ACCEPTED,
        interval=6_000, retries=100,
    )
    assert tx_execution_succeeded(receipt), receipt
    votes = receipt.get("consensus_data", {}).get("votes", {})
    assert "agree" in votes.values() and "disagree" not in votes.values(), votes
    return receipt


def send_once(client, record, state, step, send, *, finalized=False):
    if step not in state:
        state[step] = {"submission_started_at": int(time.time())}
        save(record, state)
        tx_id = send()
        state[step]["transaction_id"] = str(tx_id)
        save(record, state)
    if "transaction_id" not in state[step]:
        raise RuntimeError(
            f"{step} submission is ambiguous. Inspect the sponsor's transactions "
            "and add the confirmed transaction_id to the recovery record before resuming."
        )
    tx_id = state[step]["transaction_id"]
    print(f"{step}: {tx_id}", flush=True)
    receipt = confirmed(client, tx_id, finalized=finalized)
    state[step]["execution_succeeded"] = True
    state[step]["votes"] = receipt["consensus_data"]["votes"]
    save(record, state)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", action="store_true", help="Poll IDs in the existing recovery record")
    parser.add_argument("--smoke-test", action="store_true", help="Escrow 0.001 GEN and claim against live GitHub stars")
    parser.add_argument("--record", type=Path, default=ROOT / "artifacts/studionet-deployment.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    account = create_account(account_private_key=os.environ["DEPLOYER_PRIVATE_KEY"])
    # Hosted Studio exposes the Studio receipt API used by genlayer-test's
    # hosted client. Copy the config to avoid modifying SDK global defaults.
    client = create_client(chain=deepcopy(localnet), endpoint=STUDIO_RPC, account=account)
    source = SOURCE.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if args.record.exists():
        if not args.resume:
            parser.error("recovery record already exists; use --resume or choose a new --record")
        state = json.loads(args.record.read_text())
        if state["source_sha256"] != digest or state["owner"] != account.address:
            parser.error("recovery record source/owner differs from this deployment")
    else:
        if args.resume:
            parser.error("no recovery record exists to resume")
        state = {"network": "studionet", "owner": account.address, "source_sha256": digest}

    deployed = send_once(client, args.record, state, "deployment", lambda: client.deploy_contract(code=source, args=[]))
    address = extract_contract_address(deployed)
    state["address"] = address
    save(args.record, state)
    remote_code = client.provider.make_request("gen_getContractCode", [address])["result"]
    assert base64.b64decode(remote_code) == source, "deployed source differs from local source"
    config = client.read_contract(address=address, function_name="get_config", args=[])
    assert config["owner"] == account.address, config
    assert config["defer_seconds"] == 21_600 and config["cancel_window_seconds"] == 3_600, config
    state["config"] = config
    state["source_verified"] = True
    save(args.record, state)

    if args.smoke_test:
        reward = 10**15
        if "smoke_create" not in state:
            assert client.get_balance(account.address) >= reward, "fund the Studio sponsor before escrowing a reward"
        if "smoke_deadline" not in state:
            state["smoke_deadline"] = int(time.time()) + 7 * 86_400
            save(args.record, state)
        created = send_once(client, args.record, state, "smoke_create", lambda: client.write_contract(
            address=address, function_name="create_milestone",
            args=[account.address, "https://github.com/ethereum/go-ethereum", "github", "stars",
                  5_000, state["smoke_deadline"], "Verified deployment smoke test", 0, 0, 0], value=reward,
        ))
        mid = json.loads(created["consensus_data"]["leader_receipt"][0]["result"]["payload"]["readable"])
        claimed = send_once(client, args.record, state, "smoke_claim", lambda: client.write_contract(
            address=address, function_name="submit_claim", args=[mid],
        ), finalized=True)
        for child in claimed.get("triggered_transactions", []):
            receipt = client.wait_for_transaction_receipt(child, status=TransactionStatus.FINALIZED, interval=6_000, retries=100)
            assert receipt.get("type") == 0 and receipt.get("value_credited") is True, receipt
        milestone = client.read_contract(address=address, function_name="get_milestone", args=[mid])
        treasury = client.read_contract(address=address, function_name="get_treasury", args=[])
        assert milestone["status"] == "achieved" and milestone["payout_atto"] == reward, milestone
        assert treasury["balance_atto"] == 0 and treasury["escrowed_atto"] == 0, treasury
        state["smoke_milestone"] = milestone
        state["treasury"] = treasury
        state["smoke_verified"] = True
    state["verified_at"] = int(time.time())
    save(args.record, state)
    print(f"Verified StudioNet contract: {address}")
    print(f"Recovery record: {args.record}")


if __name__ == "__main__":
    main()

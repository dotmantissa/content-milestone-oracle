"""Wrapper constructor calldata and authority, independent of emitted-call mocks."""
import pytest
from conftest import GENVM_RELEASE

PATH = "contracts/grant_programme.py"


def test_wrapper_deploys_from_sdk_hex_string_calldata(direct_deploy, direct_vm,
        direct_owner, direct_alice):
    direct_vm.sender = direct_owner
    wrapper = direct_deploy(PATH, direct_alice.as_hex, sdk_version=GENVM_RELEASE)
    assert wrapper.oracle == direct_alice
    assert wrapper.owner == direct_owner
    assert int(wrapper.received_atto) == 0


@pytest.mark.parametrize("method,args", [
    ("create_grant", ["https://github.com/ethereum/go-ethereum", 1, 100000, 100]),
    ("cancel_grant", ["m1"]),
    ("recover_oracle_deposit", ["0x" + "1" * 64]),
    ("redeposit_budget", [100]),
    ("withdraw_budget", [100, "eoa"]),
])
def test_other_users_cannot_spend_or_recover_programme_budget(direct_deploy,
        direct_vm, direct_owner, direct_alice, method, args):
    direct_vm.sender = direct_owner
    wrapper = direct_deploy(PATH, direct_alice.as_hex, sdk_version=GENVM_RELEASE)
    direct_vm.sender = direct_alice
    if method in ("create_grant", "withdraw_budget"):
        args = [direct_alice] + args
    with pytest.raises(Exception, match="programme owner only"):
        getattr(wrapper, method)(*args)


@pytest.mark.parametrize("amount", [0, -1, 101])
def test_native_budget_redeposit_requires_available_positive_amount(direct_deploy,
        direct_vm, direct_owner, direct_alice, amount):
    direct_vm.sender = direct_owner
    wrapper = direct_deploy(PATH, direct_alice.as_hex, sdk_version=GENVM_RELEASE)
    direct_vm.deal(direct_vm._contract_address, 100)
    with pytest.raises(Exception, match="insufficient native programme budget"):
        wrapper.redeposit_budget(amount)

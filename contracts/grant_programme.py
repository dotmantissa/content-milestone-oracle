# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""Runnable owner-managed grant wrapper; funding and creation are separate phases.

Wait for each finalized child receipt before advancing. Funding donates to the
programme's budget. Credits refunded by the oracle belong to this programme;
only its owner can direct their withdrawal. Earned does not mean paid.
"""
from genlayer import *


@gl.contract_interface
class Oracle:
    class View:
        def credit_of(self, beneficiary: Address) -> int: ...
        def get_milestone(self, milestone_id: str) -> dict: ...
    class Write:
        def deposit(self) -> int: ...
        def create_from_credit(self, creator: Address, content_url: str, platform_key: str,
            metric: str, threshold: int, deadline_ts: int, reward_atto: int,
            memo: str, band_bps: int, variance_bps: int, variance_floor: int) -> str: ...
        def cancel_milestone(self, milestone_id: str) -> None: ...
        def withdraw_credit(self, to: Address, amount_atto: int, recipient_kind: str) -> dict: ...
        def recover_failed_deposit(self, transaction_id: str) -> dict: ...


class GrantProgramme(gl.Contract):
    owner: Address
    oracle: Address
    received_atto: u256

    def __init__(self, oracle: str):
        self.owner = gl.message.sender_address
        self.oracle = Address(oracle)

    def _owner_only(self) -> None:
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError("[EXPECTED] programme owner only")

    @gl.public.write.payable
    def fund_oracle(self) -> None:
        """Donate to the programme's oracle budget; no rejecting input guards."""
        Oracle(self.oracle).emit(value=gl.message.value, on="finalized").deposit()

    @gl.public.write
    def create_grant(self, creator: Address, url: str, threshold: int,
                     deadline_ts: int, reward_atto: int) -> None:
        self._owner_only()
        Oracle(self.oracle).emit(on="finalized").create_from_credit(
            creator, url, "github", "stars", threshold, deadline_ts,
            reward_atto, "Programme grant", 0, 0, 0)

    @gl.public.write
    def cancel_grant(self, milestone_id: str) -> None:
        self._owner_only()
        Oracle(self.oracle).emit(on="finalized").cancel_milestone(milestone_id)

    @gl.public.write
    def recover_oracle_deposit(self, transaction_id: str) -> None:
        """Recovery must originate from the programme, which owns the deposit."""
        self._owner_only()
        Oracle(self.oracle).emit(on="finalized").recover_failed_deposit(transaction_id)

    @gl.public.write
    def redeposit_budget(self, amount_atto: int) -> None:
        """Reuse native refunds received by this programme, without another donation."""
        self._owner_only()
        if amount_atto <= 0 or amount_atto > int(self.balance):
            raise gl.vm.UserError("[EXPECTED] insufficient native programme budget")
        Oracle(self.oracle).emit(value=u256(amount_atto), on="finalized").deposit()

    @gl.public.write
    def withdraw_budget(self, to: Address, amount_atto: int, recipient_kind: str = "eoa") -> None:
        self._owner_only()
        Oracle(self.oracle).emit(on="finalized").withdraw_credit(to, amount_atto, recipient_kind)

    @gl.public.write.payable
    def receive_payment(self) -> None:
        """Accept finalized internal withdrawals and record actual native receipt."""
        self.received_atto = u256(int(self.received_atto) + int(gl.message.value))

    @gl.public.view
    def budget(self) -> dict:
        return {"oracle_credit_atto": Oracle(self.oracle).view().credit_of(gl.message.contract_address),
                "native_balance_atto": int(self.balance), "received_atto": int(self.received_atto)}

    @gl.public.view
    def earned(self, milestone_id: str) -> bool:
        return Oracle(self.oracle).view().get_milestone(milestone_id)["status"] == "achieved"

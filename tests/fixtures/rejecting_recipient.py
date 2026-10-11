# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""Live-test receiver demonstrating native credit versus failed IC execution."""
from genlayer import *


class RejectingRecipient(gl.Contract):
    def __init__(self):
        pass

    @gl.public.write.payable
    def receive_payment(self) -> None:
        raise gl.vm.UserError("[EXPECTED] recipient application rejected payment")

    @gl.public.view
    def native_balance(self) -> int:
        return int(self.balance)

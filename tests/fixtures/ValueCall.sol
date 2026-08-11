// SPDX-License-Identifier: MIT
pragma solidity ^0.4.25;

contract ValueCall {
    function pay(address recipient, uint256 amount) public {
        recipient.call.value(amount)();
    }
}

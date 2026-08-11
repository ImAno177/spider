// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.20;

import "./Ledger.sol";

contract Vault {
    Ledger public immutable ledger;

    constructor(Ledger target) {
        ledger = target;
    }

    function deposit(uint256 amount) external returns (uint256) {
        return ledger.credit(msg.sender, amount);
    }
}

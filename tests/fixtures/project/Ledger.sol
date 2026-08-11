// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.20;

contract Ledger {
    mapping(address => uint256) public balances;

    function credit(address account, uint256 amount) external returns (uint256) {
        balances[account] += amount;
        return balances[account];
    }
}

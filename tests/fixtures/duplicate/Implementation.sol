// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.20;

contract Shared {
    function run() external pure returns (uint256 value) {
        assembly {
            value := 1
        }
    }
}

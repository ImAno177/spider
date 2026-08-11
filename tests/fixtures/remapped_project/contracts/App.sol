// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.20;

import "@dep/Math.sol";

contract App {
    function double(uint256 value) external pure returns (uint256) {
        return Math.double(value);
    }
}

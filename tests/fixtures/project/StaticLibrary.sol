// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.20;

library StaticLibrary {
    function increment(uint256 value) internal pure returns (uint256) {
        return value + 1;
    }
}

contract UsesStaticLibrary {
    function next(uint256 value) external pure returns (uint256) {
        return StaticLibrary.increment(value);
    }
}

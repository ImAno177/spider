pragma solidity 0.4.25;

import "./Treasury.sol";

contract Vault {
    Treasury public treasury;

    constructor(Treasury _treasury) public {
        treasury = _treasury;
    }

    function forward(uint256 amount) public returns (uint256 result) {
        result = treasury.record(amount);
    }
}

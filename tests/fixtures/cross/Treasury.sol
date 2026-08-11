pragma solidity 0.4.25;

contract Treasury {
    uint256 public calls;

    function record(uint256 amount) external returns (uint256) {
        calls += amount;
        return calls;
    }
}

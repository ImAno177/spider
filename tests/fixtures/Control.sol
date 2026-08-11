pragma solidity 0.4.25;

contract Control {
    uint256 public value;

    function choose(bool condition) public {
        if (condition) {
            value = 1;
        } else {
            value = 2;
        }
        value += 3;
    }
}

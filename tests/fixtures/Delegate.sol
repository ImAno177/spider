pragma solidity 0.4.25;

contract Delegate {
    function forward(address callee, bytes data) public {
        require(callee.delegatecall(data));
    }
}

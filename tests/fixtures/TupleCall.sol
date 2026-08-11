pragma solidity ^0.4.24;

contract TupleCall {
    function checked(address target) public returns (bool) {
        (bool success, ) = target.call("");
        require(success);
        return success;
    }
}

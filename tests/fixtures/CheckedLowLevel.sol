pragma solidity ^0.8.20;

contract CheckedLowLevel {
    error CallFailed();

    function checkedWithRequire(address target, bytes calldata data) external {
        (bool ok, ) = target.call(data);
        require(ok);
    }

    function checkedWithRevert(address target, bytes calldata data) external {
        (bool ok, ) = target.call(data);
        if (!ok) revert CallFailed();
    }

    function unguarded(address target, bytes calldata data) external {
        target.call(data);
    }
}

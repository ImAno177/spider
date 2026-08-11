pragma solidity ^0.8.20;

interface Probe {
    function ping() external returns (uint256);
}

contract TryAssembly {
    error CallFailed();

    function checkedTry(address target) external {
        uint256 value;
        try Probe(target).ping() returns (uint256 returned) {
            value = returned;
        } catch {
            revert CallFailed();
        }
        require(value > 0);
    }

    function rawCall(address target) external {
        assembly {
            let ok := call(gas(), target, 0, 0, 0, 0, 0)
        }
    }
}

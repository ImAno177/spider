pragma solidity ^0.8.20;

// UTF-8 source anchor probe: kiểm thử byte offsets không phải character offsets.
contract AttributeBase {
    function update(uint256[] calldata values) external virtual returns (uint256) {
        return values.length;
    }
}

contract Attributes is AttributeBase {
    uint128 public constant LIMIT = 7;
    address public immutable owner;
    mapping(address => uint256[]) private queues;

    constructor() {
        owner = msg.sender;
    }

    function checkedAdd(uint256 value) external pure returns (uint256) {
        return value + 1;
    }

    function update(uint256[] calldata values) external override returns (uint256 result) {
        uint256[] memory copied = values;
        unchecked {
            result = copied.length + 1;
        }
        return result;
    }
}

pragma solidity 0.4.25;

contract SemanticsBase {
    modifier bounded(uint256 value) {
        require(value > 0);
        _;
    }

    function positive(uint256 value) internal returns (bool) {
        return value > 0;
    }
}

contract SoliditySemantics is SemanticsBase {
    struct Record { uint256 blockNumber; }
    mapping(address => Record) public records;

    function exercise(address recipient, uint256 amount) public bounded(amount) {
        require(positive(amount));
        bool sent = recipient.send(amount);
        recipient.transfer(amount);
        records[msg.sender].blockNumber = block.number;
        require(sent);
    }
}

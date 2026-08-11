pragma solidity 0.4.25;

contract Bank {
    address public owner;
    mapping(address => uint256) public balances;

    modifier onlyOwner() {
        require(msg.sender == owner);
        _;
    }

    function deposit() public payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) public onlyOwner {
        require(balances[msg.sender] >= amount);
        msg.sender.call.value(amount)();
        balances[msg.sender] -= amount;
    }
}
